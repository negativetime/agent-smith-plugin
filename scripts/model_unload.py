#!/usr/bin/env python3
"""model_unload.py — see what the local fleet is holding in RAM/VRAM, and let it go.

A local model that answered one question five minutes ago is still occupying GPU
memory: Ollama's default residency is 5 minutes per model, and anything that sets
`keep_alive: -1` (the claude-mem observer does) pins a model until something evicts
it. On the 36 GB Mac that is the whole residency problem in SKILL.md — gpt-oss:20b
(12 GB) and qwen3-coder:30b (18 GB) cannot co-reside, so a stale resident is what
makes the NEXT call pay a 20-60s swap.

    model_unload.py                      # list what's resident (reads only, changes nothing)
    model_unload.py --all                # unload every resident Ollama model
    model_unload.py --model qwen3-coder:30b --model gemma4:26b
    model_unload.py --all --keep gpt-oss:20b     # free everything except the observer's
    model_unload.py --all --lms          # also unload LM Studio's residents
    model_unload.py --all --dry-run      # print the plan, touch nothing

Env:
    OLLAMA_HOST         Ollama base URL (default http://localhost:11434)
    SMITH_UNLOAD_KEEP   comma-separated models never unloaded (same as repeated --keep)

Exit codes: 0 = nothing left to do / all requested unloads succeeded,
            1 = at least one unload failed, 2 = usage error or Ollama unreachable.

Also importable — gemini.py and smith_agent.py use these for `--unload-after`:
    resident_names(), unload(), unload_new_since()
Pure stdlib.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
HTTP_TIMEOUT = 60


def log(*a):
    print(*a, file=sys.stderr)


def _host(host=None):
    h = (host or HOST).rstrip("/")
    return h if h.startswith("http") else "http://" + h


def _gb(n):
    return f"{n / (1024 ** 3):.1f} GB" if n else "?"


# --- Ollama ----------------------------------------------------------------------

def resident(host=None):
    """Models currently loaded in Ollama, newest API shape: GET /api/ps -> {"models":[...]}.

    Returns [] (not an exception) when Ollama isn't running — a fleet that is down is
    holding no memory, which is exactly the state a caller asking "what's resident?"
    wants reported. Raises only on a malformed reply from a server that IS up.
    """
    try:
        with urllib.request.urlopen(_host(host) + "/api/ps", timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode()).get("models", []) or []
    except urllib.error.URLError:
        return []


def resident_names(host=None):
    """Just the tags, as a set — the cheap form for before/after snapshots."""
    return {m.get("name") or m.get("model") for m in resident(host)} - {None}


def unload(model, host=None):
    """Evict one model from memory now.

    Ollama's documented unload is an EMPTY request with keep_alive=0. Generation models
    take it on /api/generate; embedding models (nomic-embed-text) are not served by that
    endpoint and 400/404 there, so fall back to /api/embed with the same contract.
    Returns True if the model is no longer resident afterwards.
    """
    base = _host(host)
    attempts = [("/api/generate", {"model": model, "prompt": "", "keep_alive": 0}),
                ("/api/embed", {"model": model, "input": [], "keep_alive": 0})]
    last = None
    for path, body in attempts:
        req = urllib.request.Request(base + path, method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                r.read()
            return True
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        except urllib.error.URLError as e:
            log(f"ERROR: can't reach Ollama at {base} ({e.reason}).")
            return False
    log(f"ERROR: unload {model} failed — {last}")
    return False


def unload_new_since(before, host=None, keep=()):
    """Unload only models that became resident AFTER the `before` snapshot.

    This is the safe form for automatic cleanup: a model that was already hot when we
    started (the observer's gpt-oss:20b, or the model the user's own session is using)
    was not ours to load and is not ours to evict. Returns the list actually unloaded.
    """
    keep = set(keep)
    ours = sorted((resident_names(host) - set(before)) - keep)
    return [m for m in ours if unload(m, host)]


# --- LM Studio -------------------------------------------------------------------
#
# LM Studio is the documented Ollama-down failover lane (references/lmstudio-pilot-*.md).
# Its residents are a SEPARATE pool that /api/ps cannot see, so a "free the GPU" pass that
# only spoke to Ollama would silently leave up to a full model's worth of memory held.
# Driven through the `lms` CLI rather than the :1234 HTTP server, which has no unload route.

def lms_available():
    return shutil.which("lms") is not None


def _lms(*args):
    try:
        return subprocess.run(["lms", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"ERROR: `lms {' '.join(args)}` failed: {exc}")
        return None


def lms_resident():
    """Loaded LM Studio models, best-effort. `lms ps` is a human table, not JSON: take the
    first field of each line that looks like a HuggingFace-style `owner/model` id."""
    p = _lms("ps")
    if p is None or p.returncode != 0:
        return []
    out = []
    for line in p.stdout.splitlines():
        tok = line.split()
        if tok and "/" in tok[0] and not tok[0].startswith("-"):
            out.append(tok[0])
    return out


def lms_unload_all():
    p = _lms("unload", "--all")
    if p is None:
        return False
    if p.returncode != 0:
        log(f"ERROR: `lms unload --all` exited {p.returncode}: "
            f"{(p.stderr or p.stdout).strip()[:200]}")
        return False
    return True


# --- CLI -------------------------------------------------------------------------

def _print_resident(host, lms):
    models = resident(host)
    if not models:
        log(f"ollama: nothing resident at {_host(host)}")
    else:
        total = sum(m.get("size") or 0 for m in models)
        log(f"ollama: {len(models)} model(s) resident, {_gb(total)}")
        for m in models:
            name = m.get("name") or m.get("model")
            vram = m.get("size_vram") or 0
            where = "GPU" if vram else "CPU"
            log(f"  {name:<32} {_gb(m.get('size')):>8}  {where}"
                f"  expires {m.get('expires_at', '?')}")
    if lms:
        if not lms_available():
            log("lm studio: `lms` CLI not on PATH — skipped")
        else:
            names = lms_resident()
            log(f"lm studio: {len(names)} model(s) resident"
                + ("".join(f"\n  {n}" for n in names) if names else ""))
    return models


def main():
    ap = argparse.ArgumentParser(
        description="List or unload models resident in local model-server memory.")
    ap.add_argument("--all", action="store_true",
                    help="Unload every resident model (minus --keep).")
    ap.add_argument("--model", action="append", default=[], metavar="TAG",
                    help="Unload this model. Repeatable.")
    ap.add_argument("--keep", action="append", default=[], metavar="TAG",
                    help="Never unload this model. Repeatable; also SMITH_UNLOAD_KEEP. "
                         "Use for a model something else is deliberately keeping hot "
                         "(e.g. the claude-mem observer's gpt-oss:20b).")
    ap.add_argument("--lms", action="store_true",
                    help="Include LM Studio's separate resident pool (via the `lms` CLI).")
    ap.add_argument("--host", default=None, help="Ollama base URL (default $OLLAMA_HOST).")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="Print what would be unloaded, change nothing.")
    args = ap.parse_args()

    if args.all and args.model:
        log("ERROR: --all and --model are alternatives, not a pair.")
        sys.exit(2)

    keep = {k.strip() for k in args.keep}
    keep |= {k.strip() for k in os.environ.get("SMITH_UNLOAD_KEEP", "").split(",") if k.strip()}

    models = _print_resident(args.host, args.lms)

    if not args.all and not args.model:
        return  # listing only

    if args.all:
        targets = sorted({m.get("name") or m.get("model") for m in models} - {None} - keep)
        skipped = sorted({m.get("name") or m.get("model") for m in models} & keep)
        for s in skipped:
            log(f"keep: {s} (protected)")
    else:
        targets = [m for m in args.model if m not in keep]

    if args.dry_run:
        for t in targets:
            log(f"would unload: {t}")
        if args.lms and lms_available() and lms_resident():
            log("would unload: all LM Studio models")
        return

    failed = []
    for t in targets:
        if unload(t, args.host):
            log(f"unloaded: {t}")
        else:
            failed.append(t)

    if args.lms and lms_available() and lms_resident():
        if lms_unload_all():
            log("unloaded: all LM Studio models")
        else:
            failed.append("lm-studio")

    if not targets and not failed:
        log("nothing to unload.")
    freed = sum(m.get("size") or 0 for m in models
                if (m.get("name") or m.get("model")) in targets and
                (m.get("name") or m.get("model")) not in failed)
    if freed:
        log(f"freed ~{_gb(freed)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
