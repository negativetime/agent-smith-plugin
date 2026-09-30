#!/usr/bin/env python3
"""Unit tests for jev.py's shorthand question builder (pure stdlib, no network calls).

Covers: each of --noul/--choice/--score builds the right {"answer": {...}} shape,
malformed --criteria/--levels JSON exits with a clear error rather than a traceback,
and no shorthand flag falls through cleanly (caller uses --questions instead).
"""
import argparse
import importlib.util
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("jev", os.path.join(_HERE, "jev.py"))
jev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jev)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def args_ns(**kw):
    base = dict(noul=None, true=None, false=None, choice=None, criteria=None,
               score=None, levels=None)
    base.update(kw)
    return argparse.Namespace(**base)


# --noul builds a single "answer" noul question with the given true/false criteria.
q = jev.build_shorthand_question(args_ns(noul="Is this a greeting?", true="Hello-like.",
                                         false="Not a greeting."))
check(q == {"answer": {"type": "noul", "instructions": "Is this a greeting?",
                       "criteria": {"true": "Hello-like.", "false": "Not a greeting."}}},
     "--noul builds the right shape")

# --choice builds a choice question with parsed JSON criteria.
q = jev.build_shorthand_question(args_ns(
    choice="Which team?", criteria='{"a": "team a", "b": "team b"}'))
check(q == {"answer": {"type": "choice", "instructions": "Which team?",
                       "criteria": {"a": "team a", "b": "team b"}}},
     "--choice builds the right shape")

# --score builds a score question with parsed JSON levels, order preserved.
q = jev.build_shorthand_question(args_ns(
    score="How urgent?", levels='["can wait", "this week", "now"]'))
check(q == {"answer": {"type": "score", "instructions": "How urgent?",
                       "criteria": ["can wait", "this week", "now"]}},
     "--score builds the right shape, level order preserved")

# No shorthand flag at all -> None, so the caller falls through to --questions/-file.
check(jev.build_shorthand_question(args_ns()) is None,
     "no shorthand flag returns None (falls through to --questions)")


def expect_exit(fn, code, what):
    global all_pass
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        fn()
        ok = False
    except SystemExit as e:
        ok = (e.code == code)
    finally:
        sys.stderr = old_stderr
    all_pass &= ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# --noul without --true/--false is a usage error (exit 2), not a KeyError.
expect_exit(lambda: jev.build_shorthand_question(args_ns(noul="Is this X?")), 2,
           "--noul without --true/--false exits 2, not a crash")

# --choice with malformed JSON --criteria exits 2 with a clear message, not a traceback.
expect_exit(lambda: jev.build_shorthand_question(
    args_ns(choice="Which?", criteria="{not json")), 2,
           "--choice with malformed --criteria JSON exits 2, not a traceback")

# --score with malformed JSON --levels exits 2 with a clear message, not a traceback.
expect_exit(lambda: jev.build_shorthand_question(
    args_ns(score="How?", levels="[not json")), 2,
           "--score with malformed --levels JSON exits 2, not a traceback")


# _summarize_answer reads each answer type's own distinguishing field.
check(jev._summarize_answer("is_bug", {"type": "noul", "noul": 0.9}) == "is_bug=0.9",
     "_summarize_answer reads .noul for a noul answer")
check(jev._summarize_answer("team", {"type": "choice", "choice": "frontend", "confidence": 0.8})
     == "team=frontend (conf=0.8)", "_summarize_answer reads .choice for a choice answer")
check(jev._summarize_answer("urgency", {"type": "score", "score": 1.9, "confidence": 0.99})
     == "urgency=1.9 (conf=0.99)", "_summarize_answer reads .score for a score answer")

print()
print("ALL PASS" if all_pass else "SOME FAILED")
sys.exit(0 if all_pass else 1)
