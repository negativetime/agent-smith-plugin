#!/usr/bin/env python3
"""Summarize agent-smith delegation activity from data/usage.jsonl.

    python3 usage_report.py              # aggregates + last 10 runs
    python3 usage_report.py --last 25    # more recent rows
    python3 usage_report.py --today      # only today's runs
    python3 usage_report.py --unreviewed # the review queue: runs with no verdict
"""
import argparse
import json
import os
from collections import Counter

LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "usage.jsonl")


def run_key(r):
    """Identity of a run for verdict lookup — ts alone has only seconds
    resolution, so two runs in the same second would collide."""
    return (r.get("ts"), r.get("script"), r.get("model"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10, help="recent rows to show")
    ap.add_argument("--today", action="store_true", help="restrict to today's runs")
    ap.add_argument("--unreviewed", action="store_true",
                    help="list only runs with no verdict yet (the review queue)")
    ap.add_argument("--include-legacy", action="store_true",
                    help="with --unreviewed, also show pre-2026-07-18 rows that have no "
                         "recorded purpose (nothing to judge them against)")
    args = ap.parse_args()

    if not os.path.isfile(LEDGER):
        print(f"No ledger yet at {LEDGER} — run a delegation first.")
        return
    rows, verdicts, witnesses = [], {}, []
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("script") == "verdict":
                key = (r.get("ref_ts"), r.get("ref_script"), r.get("ref_model"))
                verdicts[key] = r  # last verdict for a run wins
            elif r.get("script") == "witness":
                witnesses.append(r)
            else:
                rows.append(r)
    all_rows = rows  # routing weights are cumulative — never filtered by --today
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
    good = [r for r in rows if (verdicts.get(run_key(r)) or {}).get("verdict") == "good"]
    bad = [r for r in rows if (verdicts.get(run_key(r)) or {}).get("verdict") == "bad"]
    if good or bad:
        rate = len(good) / (len(good) + len(bad))
        print(f"verified: {len(good)} good, {len(bad)} bad "
              f"({rate:.0%} quality on reviewed), {len(rows)-len(good)-len(bad)} unreviewed")
    else:
        print(f"verified: none yet — after reviewing a delegation, run verdict.py good|bad")
    print("by model:")
    for k, n in by_model.most_common():
        print(f"  {n:4}  {k}")

    # ---- what the fleet is actually being USED FOR -------------------------
    # Until 2026-07-18 the ledger recorded only size/speed/model, so runs were
    # indistinguishable after the fact. Records written since carry purpose/tag/
    # project; older rows show up as "(unlabeled — pre-2026-07-18)".
    by_tag = Counter(r.get("tag") or "(untagged)" for r in rows)
    labeled = sum(n for t, n in by_tag.items() if t != "(untagged)")
    if labeled:
        print(f"by task shape ({labeled}/{len(rows)} labeled):")
        for tag, n in by_tag.most_common():
            if tag == "(untagged)":
                continue
            judged = [r for r in rows if (r.get("tag") == tag)
                      and (verdicts.get(run_key(r)) or {}).get("verdict")]
            note = f"  ({len(judged)} reviewed)" if judged else ""
            print(f"  {n:4}  {tag}{note}")
    by_project = Counter(r.get("project") for r in rows if r.get("project"))
    if by_project:
        print("by project:")
        for proj, n in by_project.most_common(8):
            print(f"  {n:4}  {proj}")
    routes = {}  # (tag, model) -> [(run_ts, verdict), ...] judged runs only
    for r in all_rows:
        v = verdicts.get(run_key(r))
        if not v or v.get("verdict") not in ("good", "bad"):
            continue
        pair = (v.get("tag") or "untagged", r.get("model") or "?")
        routes.setdefault(pair, []).append((r.get("ts") or "", v["verdict"]))
    if routes:
        print("routing weights (hebbian):")
        for (tag, model), judged in sorted(routes.items(),
                                           key=lambda kv: (-len(kv[1]), kv[0])):
            judged.sort()  # run-ts order; unreviewed runs never enter the chain
            goods = sum(1 for _, verdict in judged if verdict == "good")
            bads = len(judged) - goods
            streak = 0
            for _, verdict in reversed(judged):
                if verdict != "good":
                    break
                streak += 1
            tier = ("trusted-shape (spot-check only)" if streak >= 10
                    else "light review" if streak >= 5 else "full review")
            print(f"  {tag} @ {model}: {goods} good, {bads} bad, "
                  f"{goods/len(judged):.0%} quality, streak {streak} -> {tier}")
    if witnesses:
        agree = sum(1 for w in witnesses if w.get("agree"))
        print(f"witness drift sensor: {len(witnesses)} sampled, "
              f"{agree}/{len(witnesses)} agreed ({agree/len(witnesses):.0%})")
        streaks = {}  # per primary model: consecutive agrees from most recent back
        for w in witnesses:
            m = w.get("primary_model")
            if w.get("agree"):
                streaks[m] = streaks.get(m, 0) + 1
            else:
                streaks[m] = 0
        for m, s in sorted(streaks.items()):
            interval = min(4 * (2 ** s), 256)
            print(f"  schedule: {m}  streak {s} -> witness every ~{interval} runs")
        drifts = [w for w in witnesses if not w.get("agree")]
        for w in drifts[-5:]:
            print(f"  DRIFT {w.get('ts','?')[:19]}  {w.get('primary_model')} vs "
                  f"{w.get('witness_model')} ({w.get('context')}): "
                  f"'{w.get('primary_out','')[:40]}' vs '{w.get('witness_out','')[:40]}'")
    if bad:
        print("\nverified-BAD (regression-test feed):")
        for r in bad[-10:]:
            v = verdicts[run_key(r)]
            print(f"  {r.get('ts','?')[:19]}  {r.get('script')}:{r.get('model')}  "
                  f"— {v.get('note', '(no note)')[:90]}")
    legacy_hidden = 0
    if args.unreviewed:
        shown = [r for r in rows if not (verdicts.get(run_key(r)) or {}).get("verdict")]
        # Rows written before the 2026-07-18 purpose field carry no subject line, so
        # they can't be judged after the fact — the prompt was never stored and there
        # is nothing to reconstruct it from. Showing 800 of them buries the handful
        # that ARE actionable, so they're counted and skipped unless asked for.
        if not args.include_legacy:
            identifiable = [r for r in shown
                            if r.get("purpose") or r.get("task") or r.get("tag")]
            legacy_hidden = len(shown) - len(identifiable)
            shown = identifiable
    else:
        shown = rows
    if args.unreviewed and not shown:
        print("\nnothing unreviewed with a recorded purpose."
              + (f" ({legacy_hidden} pre-2026-07-18 rows have no subject line and "
                 f"can't be reconstructed; --include-legacy to list them.)"
                 if legacy_hidden else " Every run has a verdict."))
        return
    heading = "unreviewed runs (oldest first — verdict these)" if args.unreviewed \
        else f"last {min(args.last, len(shown))} runs"
    print(f"\n{heading}:")
    for r in shown[-args.last:]:
        v = verdicts.get(run_key(r))
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
        tag = f"[{r.get('tag')}] " if r.get("tag") else ""
        print(f"  {r.get('ts', '?')[:19]}  {r.get('script')}:{r.get('model')}  {desc}{mark}")
        purpose = r.get("purpose") or r.get("task")
        if purpose:
            files = r.get("files") or r.get("file_names")
            suffix = f"  <- {', '.join(files)}" if isinstance(files, list) and files else ""
            print(f"      {tag}{' '.join(str(purpose).split())[:110]}{suffix}")
    if args.unreviewed:
        print(f"\n  verdict.py good|bad \"why\" --tag SHAPE --model M   "
              f"({len(shown)} actionable)")
        if legacy_hidden:
            print(f"  {legacy_hidden} older rows hidden: written before purposes were "
                  f"logged (2026-07-18), so there's nothing to review them against. "
                  f"--include-legacy lists them anyway.")


if __name__ == "__main__":
    main()
