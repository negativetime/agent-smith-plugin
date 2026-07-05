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
import json
import os
import sys

LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "usage.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verdict", choices=["good", "bad"])
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

    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "script": "verdict", "verdict": args.verdict, "note": args.note,
           "ref_ts": target.get("ts"), "ref_model": target.get("model"),
           "ref_script": target.get("script")}
    if args.tag:
        rec["tag"] = args.tag
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")
    desc = (f"{target.get('script')}:{target.get('model')} @ {target.get('ts')}"
            + (f" — {target.get('task', '')[:60]}" if target.get("task") else ""))
    print(f"marked {args.verdict.upper()}: {desc}"
          + (f"  [tag: {args.tag}]" if args.tag else "")
          + (f"  ({args.note})" if args.note else ""))
    if args.verdict == "bad":
        print("-> feed it back: this task shape deserves a regression test "
              "before you delegate it again.")


if __name__ == "__main__":
    main()
