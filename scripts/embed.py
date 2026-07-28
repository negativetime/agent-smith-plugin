#!/usr/bin/env python3
"""embed.py — embeddings + reranking via Cloudflare Workers AI (free tier).

The fleet had NO embedding or reranking capability until 2026-07-28: every
retrieval question was answered by Claude reading files. This closes that gap
with two models that are free on the Workers Free plan.

    # rank a corpus against a query (the useful one)
    embed.py --rerank --query "how do I reverse a linked list" --docs notes.txt --top-k 5

    # raw vectors, one JSON object per input, for your own index
    embed.py --tag classify "some text" "another text"
    embed.py --docs corpus.txt --out vectors.jsonl

Env: CF_API_TOKEN + CF_ACCOUNT_ID. Pure stdlib.

Verified shapes 2026-07-28:
  POST /ai/run/@cf/baai/bge-m3            {"text":[...]}  -> result.data = [[float]*1024]
  POST /ai/run/@cf/baai/bge-reranker-base {"query":str,"contexts":[{"text":str}]}
                                          -> result.response = [{"id":int,"score":float}] desc
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

EMBED_MODEL = "@cf/baai/bge-m3"           # 1024-dim, multilingual
RERANK_MODEL = "@cf/baai/bge-reranker-base"
# The reranker takes the whole candidate set in one request; chunk so a big corpus
# can't blow the payload, then merge on score. Scores are comparable across chunks
# because each is an independent query-document relevance, not a within-batch softmax.
RERANK_CHUNK = 100


def log(*a):
    print(*a, file=sys.stderr)


def _ledger(rec):
    """Mirror gemini.py's ledger so fleet usage stays in one place."""
    try:
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **rec}
        rec.setdefault("project", os.path.basename(os.getcwd()) or None)
        path = os.environ.get("SMITH_LEDGER") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "usage.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def cf_run(model, payload):
    tok = os.environ.get("CF_API_TOKEN")
    acct = os.environ.get("CF_ACCOUNT_ID")
    if not tok or not acct:
        log("ERROR: CF_API_TOKEN and CF_ACCOUNT_ID must be set.\n"
            "  CF_ACCOUNT_ID is the 32-hex ACCOUNT id (`wrangler whoami`) — a member id "
            "is also 32-hex and will 404 with error 7003.")
        sys.exit(2)
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}")
    body = json.dumps(payload).encode()
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {tok}", "Content-Type": "application/json",
            "User-Agent": "agent-smith/1.5"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                wait = 4 * (attempt + 1)
                log(f"HTTP {e.code}; retry in {wait}s ...")
                time.sleep(wait)
                continue
            if "Paid plan" in msg or "5035" in msg:
                log(f"ERROR: {model} needs a Workers PAID plan. bge-m3 and "
                    f"bge-reranker-base are free; GLM-5.2 is not.")
                sys.exit(1)
            # A cfat_ token that verifies fine at /accounts/$ID/tokens/verify can still
            # 401 here when it lacks the Workers AI permission — say so, since the raw
            # message ("Authentication error") reads like a bad token.
            if e.code in (401, 403):
                log(f"ERROR: HTTP {e.code} — the token is probably missing "
                    f"Account -> Workers AI -> Read. Account-owned (cfat_) tokens are "
                    f"edited under Manage Account > API Tokens, not My Profile.\n  {msg}")
                sys.exit(1)
            log(f"ERROR: HTTP {e.code}: {msg}")
            sys.exit(1)
        except urllib.error.URLError as e:
            log(f"ERROR: can't reach Cloudflare ({e}).")
            sys.exit(1)


def read_docs(path):
    """One document per non-blank line. Blank lines and # comments are skipped."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def do_rerank(args, docs, t0):
    ranked = []
    for start in range(0, len(docs), RERANK_CHUNK):
        chunk = docs[start:start + RERANK_CHUNK]
        d = cf_run(args.model or RERANK_MODEL,
                   {"query": args.query, "contexts": [{"text": t} for t in chunk]})
        for r in (d.get("result") or {}).get("response") or []:
            # ids are chunk-local — map back to the caller's original indices.
            ranked.append({"index": start + r["id"], "score": r["score"],
                           "text": chunk[r["id"]]})
    ranked.sort(key=lambda r: -r["score"])
    if args.top_k:
        ranked = ranked[:args.top_k]
    print(json.dumps(ranked, ensure_ascii=False, indent=2))
    log(f"\n--- rerank meta ---\nmodel: {args.model or RERANK_MODEL}  "
        f"candidates: {len(docs)}  returned: {len(ranked)}  "
        f"top score: {ranked[0]['score']:.4f}" if ranked else "no results")
    _ledger({"script": "embed", "backend": "cloudflare",
             "model": args.model or RERANK_MODEL, "tag": args.tag or "rerank",
             "purpose": args.purpose or f"rerank {len(docs)} docs: {args.query[:80]}",
             "candidates": len(docs), "returned": len(ranked),
             "seconds": round(time.time() - t0, 1), "status": "ok"})


def do_embed(args, texts, t0):
    vecs, dims = [], None
    for start in range(0, len(texts), RERANK_CHUNK):
        chunk = texts[start:start + RERANK_CHUNK]
        d = cf_run(args.model or EMBED_MODEL, {"text": chunk})
        data = (d.get("result") or {}).get("data") or []
        if len(data) != len(chunk):
            log(f"ERROR: asked for {len(chunk)} vectors, got {len(data)} — refusing to "
                f"emit a misaligned index.")
            sys.exit(1)
        vecs.extend(data)
        dims = len(data[0]) if data else dims
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for t, v in zip(texts, vecs):
                f.write(json.dumps({"text": t, "embedding": v}, ensure_ascii=False) + "\n")
        log(f"wrote {len(vecs)} vectors -> {args.out}")
    else:
        print(json.dumps([{"text": t, "embedding": v}
                          for t, v in zip(texts, vecs)], ensure_ascii=False))
    log(f"\n--- embed meta ---\nmodel: {args.model or EMBED_MODEL}  "
        f"vectors: {len(vecs)}  dims: {dims}")
    _ledger({"script": "embed", "backend": "cloudflare",
             "model": args.model or EMBED_MODEL, "tag": args.tag or "embed",
             "purpose": args.purpose or f"embed {len(texts)} texts",
             "vectors": len(vecs), "dims": dims,
             "seconds": round(time.time() - t0, 1), "status": "ok"})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("texts", nargs="*", help="texts to embed (or use --docs)")
    ap.add_argument("--docs", metavar="PATH", help="file, one document per line")
    ap.add_argument("--rerank", action="store_true",
                    help="rank --docs against --query instead of embedding")
    ap.add_argument("--query", help="the query, with --rerank")
    ap.add_argument("--top-k", type=int, default=None, dest="top_k")
    ap.add_argument("--model", default=None,
                    help=f"default {EMBED_MODEL} / {RERANK_MODEL} for --rerank")
    ap.add_argument("--out", default=None, help="write JSONL vectors here instead of stdout")
    ap.add_argument("--tag", default=None, help="task-shape label for the ledger")
    ap.add_argument("--purpose", default=None)
    args = ap.parse_args()
    t0 = time.time()

    docs = read_docs(args.docs) if args.docs else list(args.texts)
    if not docs:
        log("ERROR: give texts as arguments or --docs PATH.")
        sys.exit(2)
    if args.rerank:
        if not args.query:
            log("ERROR: --rerank needs --query.")
            sys.exit(2)
        do_rerank(args, docs, t0)
    else:
        do_embed(args, docs, t0)


if __name__ == "__main__":
    main()
