#!/usr/bin/env python3
"""Mark a delegation as verified-good or verified-bad in the usage ledger.

Closes the verification loop: `ok` in the ledger means COMPLETED, not CORRECT.
After you (Claude or the user) review a delegated output, record the verdict:

    python3 verdict.py good                       # marks the most recent run
    python3 verdict.py bad "wrong API, invented .close() method"
    python3 verdict.py good --model gpt-oss:20b   # most recent run of that model
    python3 verdict.py bad "hallucinated" --script smith_agent

Every `bad` is a ready-made regression test: the task shape that failed should be
added to your eval harness before delegating that shape again.
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys

import verdict_db

LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "usage.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verdict", choices=["good", "bad", "stale"],
                    help="good|bad grade the output; stale = the output no longer exists "
                         "to judge (pre-archive run, deleted workdir), so it leaves the "
                         "queue WITHOUT polluting the routing weights.")
    ap.add_argument("note", nargs="?", default="", help="why (required for bad)")
    ap.add_argument("--model", help="target the most recent run of this model")
    ap.add_argument("--script", choices=["gemini", "smith_agent", "transcribe"],
                    help="target the most recent run of this script")
    ap.add_argument("--ts", help="target the run with this exact ts")
    ap.add_argument("--tag", help="task-shape label for routing weights "
                    "(e.g. classify, draft-code, vision-triage, research, app-build)")
    args = ap.parse_args()

    if args.verdict == "bad" and not args.note:
        print("ERROR: a 'bad' verdict needs a note — say what was wrong "
              "(it becomes the regression-test description).", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(LEDGER):
        print(f"ERROR: no ledger at {LEDGER}", file=sys.stderr)
        sys.exit(2)

    runs = []
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("script") in ("verdict", "witness"):
                continue
            runs.append(r)

    target = None
    for r in reversed(runs):
        if args.ts and r.get("ts") != args.ts:
            continue
        if args.model and r.get("model") != args.model:
            continue
        if args.script and r.get("script") != args.script:
            continue
        target = r
        break
    if not target:
        print("ERROR: no matching run found.", file=sys.stderr)
        sys.exit(1)

    # ---- stale-target guard ------------------------------------------------
    # `--model` matches the ledger's model field EXACTLY, and the ledger stores
    # the RESOLVED name the backend reported ("gemini-pro-latest"), not the
    # shorthand that was typed ("pro"). A near-miss therefore does not error --
    # it silently falls through to some far older run that happened to log the
    # literal string, and the verdict lands on the wrong route. Measured
    # 2026-09-07: `--model pro` filed a fresh doc-format verdict onto a run from
    # 2026-07-20, seven weeks stale. Refuse rather than corrupt the weights.
    if not args.ts:
        try:
            age = (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(target.get("ts", ""))).days
        except (ValueError, TypeError):
            age = None
        if age is not None and age >= 1:
            print(f"REFUSED: nearest match is {age} day(s) old "
                  f"({target.get('ts')}, model={target.get('model')}).",
                  file=sys.stderr)
            print("  A fresh run should match today. This usually means --model "
                  "did not match:\n  the ledger stores the RESOLVED name "
                  "(e.g. 'gemini-pro-latest', not 'pro').", file=sys.stderr)
            print("  Confirm with: usage_report.py --last 5   then re-run with "
                  "--ts <exact ts>.", file=sys.stderr)
            sys.exit(1)

    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "script": "verdict", "verdict": args.verdict, "note": args.note,
           "ref_ts": target.get("ts"), "ref_model": target.get("model"),
           "ref_script": target.get("script")}
    if args.tag:
        rec["tag"] = args.tag

    # ---- incident id -------------------------------------------------------
    # One BUG graded across N runs is one incident, not N failures. Measured
    # 2026-08-15: `translate` carried 20 bad rows with a byte-identical note, all
    # written in the same second — a back-fill over runs from three days earlier.
    # Every other tag was 1.0x. Routing weights and gap_report read these counts,
    # so that single incident was suppressing the route 20x harder than it earned.
    # Identical note text => same incident, so downstream can weigh events not rows.
    if args.note:
        norm = " ".join(args.note.split()).casefold()
        rec["incident"] = hashlib.sha1(norm.encode()).hexdigest()[:10]
        # `runs` above deliberately excludes verdict rows, so re-scan for prior verdicts.
        prior = []
        with open(LEDGER) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("script") != "verdict":
                    continue
                note = r.get("note") or ""
                same = (r.get("incident") == rec["incident"] if r.get("incident")
                        else " ".join(note.split()).casefold() == norm)
                if same and note:
                    prior.append(r)
        if prior:
            rec["bulk_of"] = prior[0].get("ts")
            print(f"note: identical to {len(prior)} earlier verdict(s) — recorded as the "
                  f"SAME incident ({rec['incident']}), first seen {prior[0].get('ts')}. "
                  f"Routing weights count incidents, not rows.", file=sys.stderr)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")
    try:
        verdict_db.insert(rec)
    except sqlite3.Error as e:
        print(f"warning: verdicts.db insert failed ({e}); jsonl ledger is unaffected",
              file=sys.stderr)
    desc = (f"{target.get('script')}:{target.get('model')} @ {target.get('ts')}"
            + (f" — {target.get('task', '')[:60]}" if target.get("task") else ""))
    print(f"marked {args.verdict.upper()}: {desc}"
          + (f"  [tag: {args.tag}]" if args.tag else "")
          + (f"  ({args.note})" if args.note else ""))
    if args.verdict == "bad":
        print("-> feed it back: this task shape deserves a regression test "
              "before you delegate it again.")
    if args.verdict == "stale":
        print("-> not a grade: no output survives for this run, so it counts toward "
              "neither good nor bad. Runs from 2026-07-28 on are archived and reviewable.")


if __name__ == "__main__":
    main()
