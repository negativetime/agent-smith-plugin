#!/usr/bin/env python3
"""Unit test for the --batch empty-answer guard in gemini.py (pure stdlib, no model calls).

Measured 2026-09-06: a 10-image vision prescreen wrote four 0-byte files and still reported
{"batch": 10, "ok": 10, "failed": []} — the strongest possible success for no output at all.
An empty answer is a FAILURE, and this pins that it is counted as one. The generator, the
witness sensor and the ledger are stubbed, so nothing here touches a model or the real ledger.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def run_batch_with(answer):
    """Run a 2-item batch whose backend always returns `answer`; return the JSON summary."""
    d = tempfile.mkdtemp(prefix="batch_empty_")
    paths = []
    for name in ("one.txt", "two.txt"):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("input text for this item")
        paths.append(p)
    manifest = os.path.join(d, "manifest.txt")
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(paths) + "\n")
    args = types.SimpleNamespace(
        backend="ollama", model="qwen3-coder:30b", base_url=None, consensus=None,
        batch=manifest, out_dir=os.path.join(d, "out"), file=[], system=None,
        tag="classify", no_tailor=True, temperature=0, max_tokens=None, purpose=None,
        think=None, allow_metered=False, search=False, schema=None, json=False)
    gemini._batch_generate = lambda *a, **k: answer
    gemini._witness = lambda *a, **k: None          # never re-run anything locally
    gemini._ledger = lambda *a, **k: None           # never touch the real usage ledger
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        gemini.run_batch(args, "classify this")
    return json.loads(out.getvalue().strip().splitlines()[-1]), args.out_dir


s, _ = run_batch_with("a real answer")
check(s["ok"] == 2 and s["failed_count"] == 0,
      f"non-empty answers -> ok={s['ok']}, failed={s['failed_count']}")

s, out_dir = run_batch_with("   \n  ")
check(s["ok"] == 0 and s["failed_count"] == 2,
      f"whitespace-only answers -> ok={s['ok']}, failed={s['failed_count']} (the 09-06 bug reported ok=2)")
written = [f for f in os.listdir(out_dir) if f.endswith(".out.txt")] if os.path.isdir(out_dir) else []
check(not written, f"no 0-byte .out.txt left behind (found {written})")

sys.exit(0 if all_pass else 1)
