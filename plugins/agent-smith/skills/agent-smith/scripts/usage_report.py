#!/usr/bin/env python3
"""Summarize agent-smith delegation activity from data/usage.jsonl.

    python3 usage_report.py            # aggregates + last 10 runs
    python3 usage_report.py --last 25  # more recent rows
    python3 usage_report.py --today    # only today's runs
"""
import argparse
import json
import os
from collections import Counter

LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "usage.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10, help="recent rows to show")
    ap.add_argument("--today", action="store_true", help="restrict to today's runs")
    args = ap.parse_args()

    if not os.path.isfile(LEDGER):
        print(f"No ledger yet at {LEDGER} — run a delegation first.")
        return
    rows, verdicts = [], {}
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("script") == "verdict":
                verdicts[r.get("ref_ts")] = r  # last verdict for a run wins
            else:
                rows.append(r)
    if args.today:
        import datetime
        today = datetime.date.today().isoformat()
        rows = [r for r in rows if r.get("ts", "").startswith(today)]
    if not rows:
        print("No runs recorded" + (" today." if args.today else "."))
        return

    by_model = Counter(f'{r.get("script")}:{r.get("model")}' for r in rows)
    agentic = [r for r in rows if r.get("script") == "smith_agent"]
    finished = sum(1 for r in agentic if r.get("finished"))
    secs = sum(r.get("seconds") or 0 for r in rows)
    tok_out = sum(r.get("tokens_out") or 0 for r in rows)

    print(f"ledger: {LEDGER}")
    print(f"runs: {len(rows)}  |  wall-clock delegated: {secs/60:.1f} min"
          + (f"  |  gemini output tokens: {tok_out:,}" if tok_out else ""))
    if agentic:
        print(f"agentic (smith_agent): {len(agentic)} runs, "
              f"{finished} finished ({finished/len(agentic):.0%}); "
              f"stops: {dict(Counter(r.get('stop') for r in agentic))}")
    good = [r for r in rows if (verdicts.get(r.get("ts")) or {}).get("verdict") == "good"]
    bad = [r for r in rows if (verdicts.get(r.get("ts")) or {}).get("verdict") == "bad"]
    if good or bad:
        rate = len(good) / (len(good) + len(bad))
        print(f"verified: {len(good)} good, {len(bad)} bad "
              f"({rate:.0%} quality on reviewed), {len(rows)-len(good)-len(bad)} unreviewed")
    else:
        print(f"verified: none yet — after reviewing a delegation, run verdict.py good|bad")
    print("by model:")
    for k, n in by_model.most_common():
        print(f"  {n:4}  {k}")
    if bad:
        print("\nverified-BAD (regression-test feed):")
        for r in bad[-10:]:
            v = verdicts[r.get("ts")]
            print(f"  {r.get('ts','?')[:19]}  {r.get('script')}:{r.get('model')}  "
                  f"— {v.get('note', '(no note)')[:90]}")
    print(f"\nlast {min(args.last, len(rows))} runs:")
    for r in rows[-args.last:]:
        v = verdicts.get(r.get("ts"))
        mark = {"good": " ✓", "bad": " ✗"}.get((v or {}).get("verdict"), "")
        if r.get("script") == "smith_agent":
            desc = (f"{'PASS' if r.get('finished') else 'fail'} "
                    f"{r.get('turns')}t {r.get('seconds')}s stop={r.get('stop')} "
                    f"— {' '.join((r.get('task') or '').split())[:60]}")
        elif r.get("batch"):
            desc = (f"{r.get('status')} BATCH {r.get('ok')}/{r.get('batch')} ok "
                    f"{r.get('seconds')}s"
                    + (f" ({r.get('images')} images)" if r.get("images") else ""))
        else:
            desc = (f"{r.get('status')} {r.get('seconds')}s "
                    f"in={r.get('prompt_chars')}ch out={r.get('out_chars')}ch"
                    + (f" ({r.get('images')} img)" if r.get("images") else ""))
        print(f"  {r.get('ts', '?')[:19]}  {r.get('script')}:{r.get('model')}  {desc}{mark}")


if __name__ == "__main__":
    main()
