#!/usr/bin/env python3
"""Mark a delegation as verified-good or verified-bad in the usage ledger.

Closes the verification loop: `ok` in the ledger means COMPLETED, not CORRECT.
After you (Claude or the user) review a delegated output, record the verdict:

    python3 verdict.py good                       # marks the most recent run
    python3 verdict.py bad "wrong API, invented .close() method"
    python3 verdict.py good --model gpt-oss:20b   # most recent run of that model
    python3 verdict.py bad "hallucinated" --script smith_agent
    python3 verdict.py good --run-id 3f9c2a7b1e04   # one run, when several share a ts

A grade must name one run. When the ts (or "most recent") fits several runs, as parallel
runs finishing in the same second do, verdict.py refuses and lists their run_ids.

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
    ap.add_argument("--script", choices=["gemini", "smith_agent", "transcribe", "jev"],
                    help="target the most recent run of this script")
    ap.add_argument("--ts", help="target the run with this exact ts")
    ap.add_argument("--run-id", help="target the run with this run_id (rows since 2026-10-28 carry "
                    "one; use it when several runs share a ts)")
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
            if r.get("script") in ("verdict", "witness", "cascade-attempt"):
                continue
            runs.append(r)

    cands = [r for r in runs
             if (not args.run_id or r.get("run_id") == args.run_id)
             and (not args.ts or r.get("ts") == args.ts)
             and (not args.model or r.get("model") == args.model)
             and (not args.script or r.get("script") == args.script)]
    if not cands:
        print("ERROR: no matching run found.", file=sys.stderr)
        sys.exit(1)
    target = cands[-1]

    # ---- one run, not one second -------------------------------------------
    # ts has only seconds resolution. Parallel runs finishing in the same second all fit
    # --ts, and "most recent" picked whichever was written last, so a grade could land on
    # a sibling of the run that was read (found 2026-10-28: the Lab 06 regrade could not
    # tell which of two same-second runs a grade was for). Rows since then carry a
    # run_id: refuse, and list them, rather than guess.
    same = [target] if args.run_id else [r for r in cands if r.get("ts") == target.get("ts")]
    by_id = all(r.get("run_id") for r in same)
    if len(same) > 1 and (by_id or len({(r.get("script"), r.get("model")) for r in same}) > 1):
        print(f"REFUSED: {len(same)} runs fit ({target.get('ts')}), and a grade must name "
              f"one of them:", file=sys.stderr)
        for r in same:
            what = " ".join((r.get("purpose") or r.get("task") or "").split())[:60]
            pick = (f"--run-id {r['run_id']}" if by_id
                    else f"--script {r.get('script')} --model {r.get('model')}")
            print(f"  {pick}  {r.get('script')}:{r.get('model')}  [{r.get('tag') or 'untagged'}]  "
                  f"{what}" + (f"  {r['output_file']}" if r.get("output_file") else ""), file=sys.stderr)
        print("  Re-run naming the run you read.", file=sys.stderr)
        sys.exit(1)
    if len(same) > 1:
        print(f"warning: {len(same)} runs of {target.get('script')}:{target.get('model')} finished "
              f"at {target.get('ts')} and predate run ids, so nothing can tell them apart; the "
              f"grade names all of them.", file=sys.stderr)

    # ---- stale-target guard ------------------------------------------------
    # `--model` matches the ledger's model field EXACTLY, and the ledger stores
    # the RESOLVED name the backend reported ("gemini-pro-latest"), not the
    # shorthand that was typed ("pro"). A near-miss therefore does not error --
    # it silently falls through to some far older run that happened to log the
    # literal string, and the verdict lands on the wrong route. Measured
    # 2026-09-07: `--model pro` filed a fresh doc-format verdict onto a run from
    # 2026-07-20, seven weeks stale. Refuse rather than corrupt the weights.
    if not args.ts and not args.run_id:
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
    if target.get("run_id"):
        rec["ref_run_id"] = target["run_id"]
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
            + (f" [{target['run_id']}]" if target.get("run_id") else "")
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
