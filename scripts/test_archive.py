#!/usr/bin/env python3
"""Probes for where a run's answer is saved (gemini.py _archive_output). Pure stdlib, no model calls.

Found 2026-10-28 by the Lab 06 blind regrade: the file was named <ts>-<script>-<model>.txt and
opened with "w", and ts has only seconds resolution, so two runs of one model finishing in the
same second wrote one file. The second answer replaced the first, both ledger rows named the
survivor, and nothing said so. One sampled run had to be set aside because which answer had
been graded could not be known.

    python3 -B scripts/test_archive.py
"""
import importlib.util
import os
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


REC = {"ts": "2026-08-31T20:07:38", "script": "gemini", "model": "glm-5.3"}
os.environ.pop("SMITH_NO_ARCHIVE", None)

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "usage.jsonl")
    a = gemini._archive_output(ledger, REC, "first answer")
    b = gemini._archive_output(ledger, REC, "second answer")
    check(a == os.path.join("outputs", "2026-08-31", "2026-08-31T20_07_38-gemini-glm-5.3.txt"),
          "the first run in a second keeps the old name")
    check(a and b and a != b, "a second run in the same second gets its own file")
    read = lambda p: open(os.path.join(tmp, p), encoding="utf-8").read()
    check(a and read(a) == "first answer", "the first answer is still there after the second is saved")
    check(b and read(b) == "second answer", "the second answer is in the file its row names")

with tempfile.TemporaryDirectory() as tmp:
    # Parallel hand-offs (fanout, the regrade's four in flight) finish at once: the name has to be
    # taken atomically, not checked and then written.
    ledger = os.path.join(tmp, "usage.jsonl")
    paths, start = [None] * 12, threading.Barrier(12)

    def save(i):
        start.wait()
        paths[i] = gemini._archive_output(ledger, REC, f"answer {i}")

    threads = [threading.Thread(target=save, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check(None not in paths and len(set(paths)) == 12, "12 runs saved at once get 12 files")
    check(all(open(os.path.join(tmp, p), encoding="utf-8").read() == f"answer {i}" for i, p in enumerate(paths)),
          "each of the 12 files holds its own run's answer")

with tempfile.TemporaryDirectory() as tmp:
    os.environ["SMITH_NO_ARCHIVE"] = "1"
    check(gemini._archive_output(os.path.join(tmp, "usage.jsonl"), REC, "x") is None
          and not os.path.exists(os.path.join(tmp, "outputs")), "SMITH_NO_ARCHIVE=1 still saves nothing")
    os.environ.pop("SMITH_NO_ARCHIVE")

sys.exit(0 if all_pass else 1)
