#!/usr/bin/env python3
"""
gemini.py — shell out to Google Gemini using the user's GEMINI_API_KEY.

Purpose: offload bulk / large-context / research work to Gemini so it doesn't
burn Claude tokens. Answer goes to STDOUT; model, token usage, and any grounding
sources go to STDERR (so captured output stays clean).

Pure stdlib — no pip installs required.

Examples:
  python3 gemini.py "Summarize the theory of relativity in 5 bullets" --model flash
  echo "long prompt text..." | python3 gemini.py --model pro
  python3 gemini.py --search "What changed in the latest macOS Tahoe release?"
  python3 gemini.py --file report.pdf "Extract every dollar figure as a markdown table"
  python3 gemini.py --file a.csv --file b.csv --json "Merge these and list duplicate rows"
  python3 gemini.py --list-models
"""
import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com"
INLINE_LIMIT = 15 * 1024 * 1024  # files larger than this go through the Files API

# Friendly aliases. Pass any real model name through unchanged.
ALIASES = {
    "flash": "gemini-flash-latest",
    "pro": "gemini-pro-latest",
    "flash-lite": "gemini-flash-lite-latest",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def api_key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        log("ERROR: GEMINI_API_KEY is not set. Two options: (1) get a free key at "
            "https://aistudio.google.com/apikey and `export GEMINI_API_KEY=...`, or "
            "(2) skip the account entirely and run local — `--backend ollama` (set up a model "
            "with scripts/setup_local_model.sh, e.g. Gemma for general or qwen2.5-coder for code).")
        sys.exit(2)
    return k


def http(url, method="GET", data=None, headers=None, timeout=300):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url, method="GET", body=None, headers=None, timeout=300, retries=4):
    """POST/GET JSON with retry/backoff on 429 + 5xx."""
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    payload = json.dumps(body).encode() if body is not None else None
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with http(url, method, payload, h, timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if e.code in (429, 500, 503) and attempt < retries:
                log(f"  [retry] HTTP {e.code} (attempt {attempt + 1}/{retries}); waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            log(f"ERROR: HTTP {e.code} from Gemini:\n{err[:2000]}")
            sys.exit(1)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                log(f"  [retry] network error: {e}; waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            log(f"ERROR: network failure talking to Gemini: {e}")
            sys.exit(1)


def guess_mime(path):
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    # Sniff: treat decodable bytes as plain text, else generic binary.
    try:
        with open(path, "rb") as f:
            f.read(2048).decode("utf-8")
        return "text/plain"
    except Exception:
        return "application/octet-stream"


def upload_file(path, key):
    """Resumable Files API upload for large files. Returns (uri, mime)."""
    with open(path, "rb") as f:
        data = f.read()
    mime = guess_mime(path)
    name = os.path.basename(path)
    start = http(
        f"{BASE}/upload/v1beta/files?key={key}",
        method="POST",
        data=json.dumps({"file": {"display_name": name}}).encode(),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    upload_url = start.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        log("ERROR: Files API did not return an upload URL.")
        sys.exit(1)
    with http(
        upload_url,
        method="POST",
        data=data,
        headers={
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(len(data)),
        },
    ) as r:
        info = json.loads(r.read().decode())
    fobj = info["file"]
    fname, uri, state = fobj["name"], fobj["uri"], fobj.get("state", "ACTIVE")
    while state == "PROCESSING":
        time.sleep(2)
        with http(f"{BASE}/v1beta/{fname}?key={key}") as r:
            fobj = json.loads(r.read().decode())
        state = fobj.get("state", "ACTIVE")
    if state == "FAILED":
        log(f"ERROR: Gemini failed to process file {name}.")
        sys.exit(1)
    log(f"  [file] uploaded {name} ({len(data)} bytes) -> {uri}")
    return uri, fobj.get("mimeType", mime)


def make_file_part(path, key):
    size = os.path.getsize(path)
    mime = guess_mime(path)
    if size <= INLINE_LIMIT:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return {"inlineData": {"mimeType": mime, "data": b64}}
    uri, mime = upload_file(path, key)
    return {"fileData": {"mimeType": mime, "fileUri": uri}}


def extract_text(candidate):
    parts = candidate.get("content", {}).get("parts", []) or []
    out = []
    for p in parts:
        if p.get("thought"):  # skip internal thinking parts
            continue
        if "text" in p:
            out.append(p["text"])
    return "".join(out)


def grounding_sources(candidate):
    md = candidate.get("groundingMetadata", {})
    chunks = md.get("groundingChunks", []) or []
    srcs = []
    for c in chunks:
        web = c.get("web", {})
        if web.get("uri"):
            srcs.append((web.get("title", "").strip(), web["uri"]))
    return srcs


def resolve_fm_helper():
    """Find the Apple FM sidecar binary: $FM_HELPER, then next to this script, then PATH."""
    env = os.environ.get("FM_HELPER")
    if env and os.path.exists(env):
        return env
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fm_helper")
    if os.path.exists(local):
        return local
    return shutil.which("fm_helper")


def call_fm(prompt, system, temperature):
    """Apple Foundation Models, on-device (macOS 26+). Free, offline, private, no quota."""
    helper = resolve_fm_helper()
    if not helper:
        log("ERROR: Apple FM helper (fm_helper) not found next to this script or on PATH. "
            "Set FM_HELPER=/path/to/fm_helper. Requires macOS 26+ with Apple Intelligence on.")
        sys.exit(2)
    req = {"messages": [{"role": "user", "content": prompt}]}
    if system:
        req["system"] = system
    if temperature is not None:
        req["temperature"] = temperature
    try:
        out = subprocess.run([helper], input=json.dumps(req).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except Exception as e:
        log(f"ERROR: could not run fm_helper: {e}")
        sys.exit(1)
    if out.returncode != 0:
        log(f"ERROR: fm_helper exited {out.returncode}: {out.stderr.decode('utf-8', 'replace')[:500]}")
        sys.exit(1)
    try:
        d = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        log(f"ERROR: unexpected fm_helper output: {out.stdout.decode('utf-8', 'replace')[:500]}")
        sys.exit(1)
    if d.get("error"):
        log(f"ERROR: Apple FM: {d['error']}")
        sys.exit(1)
    log("\n--- fm meta ---")
    log("backend: apple-foundation-models (on-device · free · offline · private)")
    return d.get("answer", "")


def call_ollama(prompt, system, temperature, model, max_tokens):
    """Local model via Ollama (http://localhost:11434). Free, unlimited, offline."""
    model = model or os.environ.get("OLLAMA_MODEL") or "qwen2.5-coder:14b"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "stream": False}
    opts = {}
    if temperature is not None:
        opts["temperature"] = temperature
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if opts:
        body["options"] = opts
    req = urllib.request.Request("http://localhost:11434/api/chat", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        log(f"ERROR: Ollama HTTP {e.code}: {msg[:300]}. Is '{model}' pulled? Try: ollama pull {model}")
        sys.exit(1)
    except urllib.error.URLError as e:
        log(f"ERROR: can't reach Ollama ({e}). Start it: `ollama serve`, then `ollama pull {model}`.")
        sys.exit(1)
    text = resp.get("message", {}).get("content", "")
    log("\n--- ollama meta ---")
    log(f"backend: ollama  model: {model}  (local · free · unlimited)")
    pe, ec = resp.get("prompt_eval_count"), resp.get("eval_count")
    if pe is not None or ec is not None:
        log(f"tokens: prompt={pe} output={ec}")
    return text


def main():
    # Force UTF-8 on stdout/stderr so Gemini's Unicode output (em-dashes, accents, etc.)
    # prints cleanly on Windows consoles, which often default to cp1252.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Query Gemini from the shell.")
    ap.add_argument("prompt", nargs="?", help="Prompt text. If omitted, read from stdin.")
    ap.add_argument("--backend", choices=["gemini", "fm", "ollama"], default="gemini",
                    help="gemini (cloud, default — files/grounding/JSON) | fm (Apple on-device, "
                         "free/offline/private) | ollama (local model, free/unlimited/offline).")
    ap.add_argument("--model", default=None,
                    help="gemini: flash|pro|flash-lite|<name> (default flash). "
                         "ollama: model tag (default qwen2.5-coder:14b). Ignored for fm.")
    ap.add_argument("--system", help="System instruction (role/style/constraints).")
    ap.add_argument("--file", action="append", default=[], metavar="PATH",
                    help="Attach a file (PDF/image/text/csv/...). Repeatable.")
    ap.add_argument("--search", "--grounding", dest="search", action="store_true",
                    help="Enable live Google Search grounding (for research / fresh facts).")
    ap.add_argument("--json", action="store_true", help="Force a JSON response.")
    ap.add_argument("--schema", metavar="PATH", help="Path to a JSON Schema file (implies --json).")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    ap.add_argument("--thinking-budget", type=int, default=None, dest="thinking_budget",
                    help="Token budget for model 'thinking' (0 = off, faster/cheaper on Flash).")
    ap.add_argument("--list-models", action="store_true", help="List models this key can use, then exit.")
    args = ap.parse_args()

    if args.list_models:
        key = api_key()
        d = http_json(f"{BASE}/v1beta/models?key={key}")
        for m in d.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"].replace("models/", ""))
        return

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip() and not args.file:
        log("ERROR: no prompt given (pass an argument or pipe text on stdin).")
        sys.exit(2)

    # Non-Gemini backends: on-device Apple FM, or a local Ollama model. Both are text-only
    # here — file ingest and web grounding stay on the gemini backend (where they're free).
    if args.backend in ("fm", "ollama"):
        if args.file:
            log(f"ERROR: --file is only on the gemini backend (PDFs/images need Gemini). "
                f"Use --backend gemini, or paste text into the prompt for {args.backend}.")
            sys.exit(2)
        if args.search:
            log("ERROR: --search (web grounding) is only on the gemini backend.")
            sys.exit(2)
        if args.backend == "fm":
            text = call_fm(prompt, args.system, args.temperature)
        else:
            text = call_ollama(prompt, args.system, args.temperature, args.model, args.max_tokens)
        print(text)
        sys.stdout.flush()
        return

    # --- gemini backend (default) ---
    key = api_key()
    model = ALIASES.get(args.model or "flash", args.model or "flash")

    parts = []
    for fp in args.file:
        if not os.path.exists(fp):
            log(f"ERROR: file not found: {fp}")
            sys.exit(2)
        parts.append(make_file_part(fp, key))
    if prompt.strip():
        parts.append({"text": prompt})

    body = {"contents": [{"role": "user", "parts": parts}]}

    if args.system:
        body["systemInstruction"] = {"parts": [{"text": args.system}]}
    if args.search:
        body["tools"] = [{"googleSearch": {}}]

    gen = {}
    if args.temperature is not None:
        gen["temperature"] = args.temperature
    if args.max_tokens is not None:
        gen["maxOutputTokens"] = args.max_tokens
    if args.thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": args.thinking_budget}
    if args.schema or args.json:
        gen["responseMimeType"] = "application/json"
    if args.schema:
        with open(args.schema) as f:
            gen["responseSchema"] = json.load(f)
    if gen:
        body["generationConfig"] = gen

    url = f"{BASE}/v1beta/models/{model}:generateContent?key={key}"
    resp = http_json(url, method="POST", body=body)

    fb = resp.get("promptFeedback", {})
    if fb.get("blockReason"):
        log(f"ERROR: Gemini blocked the prompt ({fb['blockReason']}).")
        sys.exit(1)

    cands = resp.get("candidates", [])
    if not cands:
        log(f"ERROR: empty response from Gemini.\n{json.dumps(resp)[:1500]}")
        sys.exit(1)
    cand = cands[0]
    text = extract_text(cand)
    print(text)
    sys.stdout.flush()

    # Diagnostics to stderr (Claude can read these; they don't pollute stdout).
    u = resp.get("usageMetadata", {})
    log(f"\n--- gemini meta ---")
    log(f"model: {model}")
    log(f"tokens: prompt={u.get('promptTokenCount')} output={u.get('candidatesTokenCount')} "
        f"thoughts={u.get('thoughtsTokenCount', 0)} total={u.get('totalTokenCount')}")
    fr = cand.get("finishReason")
    if fr and fr != "STOP":
        log(f"finishReason: {fr}  (output may be truncated/partial)")
    if args.search:
        srcs = grounding_sources(cand)
        if srcs:
            log("sources:")
            for title, uri in srcs:
                log(f"  - {title or '(untitled)'}: {uri}")
        else:
            log("sources: (none returned — Gemini may not have searched)")


if __name__ == "__main__":
    main()
