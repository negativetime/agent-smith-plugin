#!/usr/bin/env python3
"""Where is the fleet NOT being used that it could be? — the offload gap report.

    python3 gap_report.py                 # the gap table + ranked actions
    python3 gap_report.py --audit PATH    # a different token_audit.json
    python3 gap_report.py --refresh       # re-run token_audit.py first

The usage ledger (usage.jsonl) answers "what did the fleet do." It is
structurally blind to the more useful question — "what did Claude do that the
fleet should have done" — because work that was never delegated never reaches
it. That half lives in the Claude transcripts, which token_audit.py already
mines into token_audit.json.

This joins the two: per task shape, how much ran on Claude vs on the fleet, and
what the fleet's measured trust for that shape currently is. Two distinct kinds
of gap fall out —

  UNUSED     Claude did the work itself while a trusted fleet route sat idle.
  MISROUTED  the work was delegated, to a model the ledger says is bad at it.

Neither is visible from either dataset alone.
"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(SKILL, "data", "usage.jsonl")
AUDIT = "/Users/joshualangberg/Python/docs/token_audit.json"
AUDIT_SCRIPT = "/Users/joshualangberg/Python/docs/token_audit.py"

# Claude-side signals -> the offloadable shape they represent.
#   tools:  token_audit tool_calls names that indicate this shape
#   chars:  tool_result_chars names, when volume matters more than call count
#   tag:    the ledger tag the fleet uses for the same shape
#   route:  what it should be delegated to
SHAPES = [
    {"name": "web research",
     "tools": ["WebSearch", "WebFetch", "mcp__claude_ai_Exa__web_search_exa",
               "mcp__claude_ai_Exa__web_fetch_exa"],
     "tag": "research",
     "route": "gemini.py --search --tag research"},
    {"name": "screenshot / vision triage",
     "chars": ["Read [images]", "mcp__chrome-devtools__take_screenshot [images]"],
     "tag": "vision-prescreen",
     "route": "gemini.py --backend ollama --model qwen3-vl:4b --file shot.png"},
    {"name": "read-only subagent fan-out",
     "tools": ["Agent"],
     "tag": "subagent-fanout",
     "route": "gemini.py, or --backend ollama --batch for many items"},
    {"name": "long-document digest",
     "chars": ["Read"],
     "tag": "long-digest",
     "route": "gemini.py --backend ollama --model gpt-oss:20b (131k ctx)"},
]

# Rough cost of one avoided Claude call, for ORDERING only — not a billing
# figure. Kept deliberately crude; the point is which row is biggest, not how big.
EST_OUT_TOKENS_PER_CALL = {"web research": 1200, "read-only subagent fan-out": 8000}
EST_TOKENS_PER_MCHAR = 250_000  # ~4 chars per token

# Legacy fleet-side detection. Tags only exist on runs since 2026-07-18, so
# counting tags alone reports "0 delegated" for shapes with years of history —
# research shows 6 good verdicts on gemini-pro yet zero tagged runs. These
# per-shape predicates recover the untagged history from fields the ledger has
# always written, so the ratios aren't fiction.
def _is_research(r):
    return bool(r.get("search"))


def _is_vision(r):
    return bool(r.get("images"))


def _is_digest(r):
    files = r.get("files") or r.get("file_names")
    n = files if isinstance(files, int) else len(files or [])
    return bool(n) or (r.get("prompt_chars") or 0) > 20000


LEGACY = {"research": _is_research,
          "vision-prescreen": _is_vision,
          "long-digest": _is_digest}


def is_benchmark(r):
    """agent-gym eval traffic or routing smoke tests, not real delegated work.

    The gym drives the same gemini.py, so its evals land in this ledger. Counting
    them as fleet usage overstates delegation and shrinks the apparent gap — the
    error flatters us, which is the worst direction for it to run (2026-07-20:
    6 of the 8 most recent rows were evals). `gym-eval` tags new rows; the project
    name catches those logged before that tag existed. `smoke` covers the "say hi"
    routing checks, which would otherwise pad the research/doc-format lanes with
    runs that delegated no actual work (2026-07-28).
    """
    return r.get("tag") in ("gym-eval", "smoke") or r.get("project") == "agent-gym"


def load_ledger():
    rows, verdicts = [], {}
    if not os.path.isfile(LEDGER):
        return rows, verdicts
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("script") == "verdict":
                verdicts[(r.get("ref_ts"), r.get("ref_script"), r.get("ref_model"))] = r
            elif r.get("script") != "witness":
                rows.append(r)
    return rows, verdicts


def trust_for(tag, rows, verdicts):
    """Measured quality per model for a shape: {model: (good, bad)}."""
    out = {}
    for r in rows:
        v = verdicts.get((r.get("ts"), r.get("script"), r.get("model")))
        if not v or v.get("verdict") not in ("good", "bad"):
            continue
        if (v.get("tag") or "untagged") != tag:
            continue
        g, b = out.get(r.get("model"), (0, 0))
        out[r.get("model")] = (g + (v["verdict"] == "good"), b + (v["verdict"] == "bad"))
    return out


def still_routed(tag, model, rows, verdicts):
    """True if `tag` work was sent to `model` again AFTER the last bad verdict on it.

    Distinguishes a live misroute (keep warning) from one already closed in code or
    habit (stop warning). Timestamps are ISO-8601, so string compare is chronological.
    """
    last_bad = ""
    for r in rows:
        v = verdicts.get((r.get("ts"), r.get("script"), r.get("model")))
        if not v or v.get("verdict") != "bad":
            continue
        if r.get("model") != model or (v.get("tag") or "untagged") != tag:
            continue
        last_bad = max(last_bad, r.get("ts") or "")
    if not last_bad:
        return True
    return any(r.get("model") == model and r.get("tag") == tag
               and (r.get("ts") or "") > last_bad for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=AUDIT, help="path to token_audit.json")
    ap.add_argument("--refresh", action="store_true", help="re-run token_audit.py first")
    args = ap.parse_args()

    if args.refresh and os.path.isfile(AUDIT_SCRIPT):
        print("refreshing token audit (reads all transcripts, ~1 min)...", file=sys.stderr)
        subprocess.run([sys.executable, AUDIT_SCRIPT], capture_output=True)

    if not os.path.isfile(args.audit):
        print(f"No token audit at {args.audit}.\n"
              f"Run: python3 {AUDIT_SCRIPT}   (then re-run this)")
        return 1
    audit = json.load(open(args.audit))
    calls = dict(audit.get("tool_calls") or [])
    chars = dict(audit.get("tool_result_chars") or [])

    all_rows, verdicts = load_ledger()
    # Benchmark runs are excluded from fleet-usage counts: an eval is not a
    # delegation. Their VERDICTS still stand as graded evidence for routing.
    bench = [r for r in all_rows if is_benchmark(r)]
    rows = [r for r in all_rows if not is_benchmark(r)]
    by_tag = Counter(r.get("tag") for r in rows if r.get("tag"))
    tagged = sum(by_tag.values())

    print("OFFLOAD GAP — Claude-side work vs fleet-side work, by task shape")
    print("=" * 78)
    print(f"ledger: {len(rows)} delegated runs ({tagged} tagged)"
          + (f", {len(bench)} gym-eval runs excluded" if bench else "")
          + f"   audit: {args.audit}")
    print("Claude-side counts are ~30d of transcripts; fleet-side is ledger lifetime,")
    print("and only runs since 2026-07-18 carry tags — so ratios read as a floor.\n")

    actions = []
    for s in SHAPES:
        claude_calls = sum(calls.get(t, 0) for t in s.get("tools", []))
        claude_mchars = sum(chars.get(c, 0) for c in s.get("chars", [])) / 1e6
        tagged_n = by_tag.get(s["tag"], 0)
        legacy_pred = LEGACY.get(s["tag"])
        legacy_n = sum(1 for r in rows if not r.get("tag") and legacy_pred(r)) \
            if legacy_pred else 0
        fleet = tagged_n + legacy_n
        # all_rows on purpose: a gym eval is not fleet USAGE, but a verdict on one
        # is still graded evidence of how a model performs at that shape — often
        # better evidence than production review, since a hidden grader scored it.
        trust = trust_for(s["tag"], all_rows, verdicts)

        if s.get("tools"):
            claude_unit, claude_val = "calls", claude_calls
            est, est_kind = claude_calls * EST_OUT_TOKENS_PER_CALL.get(s["name"], 1000), "out"
        else:
            claude_unit, claude_val = "M chars", round(claude_mchars, 1)
            # Char volume is tool RESULT text — it lands in context as input and
            # gets re-carried every turn. Calling it output tokens would overstate
            # it by more than the account's entire output budget.
            est, est_kind = int(claude_mchars * EST_TOKENS_PER_MCHAR), "context"
        if not claude_val and not fleet:
            continue

        total = claude_val + fleet if claude_unit == "calls" else None
        ratio = f"{fleet/total:.0%}" if total else "n/a"

        print(f"── {s['name']}")
        print(f"     Claude did : {claude_val} {claude_unit}"
              + (f"  (~{est/1e6:.0f}M {est_kind} tokens)" if est > 2e6
                 else f"  (~{est//1000}k {est_kind} tokens)"))
        detail = f"{tagged_n} tagged" + (f" + {legacy_n} untagged-legacy" if legacy_n else "")
        print(f"     fleet did  : {fleet} run(s) [{detail}]   offload ratio: {ratio}")
        if trust:
            bits = []
            for m, (g, b) in sorted(trust.items(), key=lambda kv: -kv[1][0]):
                mark = "OK" if g and not b else ("BAD" if b and not g else "mixed")
                bits.append(f"{m} {g}g/{b}b {mark}")
            print(f"     measured   : {'; '.join(bits)}")
            # Only flag a misroute that is STILL LIVE. A model can score 0-good on a
            # shape and then be routed away from in code (gemini.py DEFAULT_MODEL_BY_TAG),
            # at which point the warning is a permanent false alarm that teaches you to
            # ignore the report. Live == the shape was actually sent there again after
            # the verdict that condemned it (2026-07-28).
            bad = [m for m, (g, b) in trust.items() if b and not g
                   if still_routed(s["tag"], m, rows, verdicts)]
            if bad:
                actions.append((0, f"MISROUTED: {s['name']} is being sent to "
                                   f"{', '.join(bad)}, which the ledger scores 0-good. "
                                   f"Reroute, don't just delegate more."))
        else:
            print(f"     measured   : no verdicts yet for this shape")
        print(f"     route      : {s['route']}")
        mag = (f"~{est/1e6:.0f}M {est_kind}" if est > 2e6 else f"~{est//1000}k {est_kind}")
        if claude_val and fleet == 0:
            actions.append((est, f"UNUSED: {claude_val} {claude_unit} of {s['name']} ran on "
                                 f"Claude, 0 delegated ({mag} tokens). {s['route']}"))
        elif claude_val and total and fleet / total < 0.25:
            actions.append((est, f"UNDERUSED: {s['name']} only {ratio} delegated "
                                 f"({mag} tokens on Claude). {s['route']}"))
        elif claude_val and not total and fleet:
            actions.append((est, f"CHECK: {s['name']} — {fleet} fleet run(s) vs {claude_val} "
                                 f"{claude_unit} on Claude ({mag} tokens); no shared unit, "
                                 f"judge by eye. {s['route']}"))
        print()

    print("RANKED ACTIONS")
    print("=" * 78)
    if not actions:
        print("  none — every mapped shape is being routed to the fleet.")
    for _, msg in sorted(actions, key=lambda a: -a[0]):
        print(f"  • {msg}")

    untracked = [t for t in by_tag if t not in {s["tag"] for s in SHAPES}]
    if untracked:
        print(f"\n  fleet shapes with no Claude-side signal mapped "
              f"(can't compute a gap for these): {', '.join(sorted(untracked))}")
    print("\n  Re-run after a token audit refresh: gap_report.py --refresh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
