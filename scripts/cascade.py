#!/usr/bin/env python3
"""Escalation ladder for agent-smith: pure logic, no model calls, no network.

gemini.py asks this module three questions and does the calling itself:
  1. which rungs, in order?            -> build_ladder()
  2. is a local rung worth trying?     -> local_unhealthy()
  3. did this rung's answer hold up?   -> check_output()

A rung that fails a MECHANICAL gate (empty, truncated, bad JSON, Python that will not
parse, a described-not-done reply) hands the same prompt to the next rung. Mechanical
only: a gate that needs judgment belongs to Claude's review, not here. A passing gate
is not a correct answer; it just means the output is worth reviewing.

    python3 cascade.py --report        # per-tag escalation rates from the ledger

Tiers (2026-10-20). T2 is the "large" tier because this Mac cannot hold a 30B+ model
beside the claude-mem observer, so the flat-rate z.ai plan fills that role:
    T0 llama3.2:3b   tiny, local          classify floor
    T1 gpt-oss:20b   local, free/private  trusted code/struct/long-digest
    T2 glm-5.3       z.ai Coding Plan     flat-rate, $0 marginal
    T3 gemini-cli    Google AI Pro login  paid already, text only
"""
import argparse
import json
import os
import sys

RUNGS = {
    "T0": {"backend": "ollama", "model": "llama3.2:3b", "local": True,
           "min_max_tokens": None},
    "T1": {"backend": "ollama", "model": "gpt-oss:20b", "local": True,
           "min_max_tokens": 8192},
    "T2": {"backend": "openai", "model": "glm-5.3", "local": False,
           "base_url": "zai-coding", "min_max_tokens": 32000, "needs_env": "ZAI_API_KEY"},
    "T3": {"backend": "gemini-cli", "model": "pro", "local": False,
           "min_max_tokens": None},
}

# Where each shape STARTS. These mirror DEFAULT_LOCAL_FOR_TAG / DEFAULT_PAID_FOR_TAG in
# gemini.py (the start rung is never invented here), and each ladder only adds rungs ABOVE
# the earned one. Unknown tags start at T2: gemini's own default for them is the rate-limited
# free API, which is a specialist lane, not a bulk one.
LADDER_BY_TAG = {
    "classify":        ["T0", "T1", "T2"],
    "subagent-fanout": ["T1", "T2"],
    "code-draft":      ["T2", "T3"],
    "long-digest":     ["T2", "T3"],
    "translate":       ["T2", "T3"],
    "copy-draft":      ["T2", "T3"],
    "doc-format":      ["T2", "T3"],
}
DEFAULT_LADDER = ["T2", "T3"]

# gpt-oss:20b is a measured Route BLOCK for doc-format (0g/3b, the silent proper-noun
# mutation). A ladder must never route a shape onto a blocked rung, whatever it was told.
BLOCKED = {("T1", "doc-format")}

LOAD_FACTOR = 1.5          # local-fleet-preflight: go cloud above ~1.5x cores


def parse_ladder(spec):
    """'T1,T2' -> ['T1','T2']. Raises ValueError on an unknown rung or a repeat."""
    names = [s.strip().upper() for s in spec.split(",") if s.strip()]
    if not names:
        raise ValueError("empty ladder")
    bad = [n for n in names if n not in RUNGS]
    if bad:
        raise ValueError(f"unknown rung(s) {bad}; known: {sorted(RUNGS)}")
    if len(set(names)) != len(names):
        raise ValueError(f"repeated rung in {names}")
    return names


def build_ladder(tag, explicit=None, start=None):
    """The rungs to try, in order. `explicit` replaces the tag's ladder; `start` drops
    every rung below it. Blocked (rung, tag) pairs are removed with no override: a
    caller who wants the blocked model can call gemini.py without --cascade."""
    names = parse_ladder(explicit) if explicit is not None else list(LADDER_BY_TAG.get(tag, DEFAULT_LADDER))
    if start:
        start = start.strip().upper()
        if start not in names:
            raise ValueError(f"--start {start} is not on the ladder {names}")
        names = names[names.index(start):]
    return [n for n in names if (n, tag) not in BLOCKED]


def local_unhealthy(load1, ncpu, ollama_up):
    """Reason a LOCAL rung should be skipped, or None if it is worth trying.
    A wedged Mac burns the timeout before it fails (measured 2026-08-08: two tokens in
    32.9 s at load 33), so the answer is no, not 'try and see'."""
    if not ollama_up:
        return "ollama not answering"
    if load1 is not None and ncpu and load1 > LOAD_FACTOR * ncpu:
        return f"load {load1:.1f} > {LOAD_FACTOR} x {ncpu} cores"
    return None


def _strip_fence(text):
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def check_output(text, finish_reason=None, want_json=False, want_python=False,
                 extra=()):
    """Return None when the output passes every mechanical gate, else a short reason.

    `extra` is a sequence of callables (text) -> reason-or-None, for gates that live in
    gemini.py (the agentic-non-answer detector)."""
    if not text or not text.strip():
        return "empty"
    if finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
        return "truncated"
    if want_json:
        try:
            json.loads(_strip_fence(text))
        except ValueError:
            return "bad-json"
    if want_python:
        import ast
        try:
            ast.parse(_strip_fence(text))
        except SyntaxError as e:
            return f"py-syntax (line {e.lineno})"
    for fn in extra:
        why = fn(text)
        if why:
            return why
    return None


def floor_max_tokens(rung, requested):
    """Raise (never lower) max_tokens to the rung's reasoning-budget floor."""
    floor = RUNGS[rung].get("min_max_tokens")
    if floor and (requested is None or requested < floor):
        return floor
    return requested


# --- report ---------------------------------------------------------------------------

def summarize(rows):
    """Per-tag escalation stats from ledger rows.

    A cascade is the set of rows sharing `cascade_id`. Attempts are script
    'cascade-attempt' (gate failed); the winner is the normal row carrying `cascade`."""
    by_id = {}
    for r in rows:
        cid = r.get("cascade_id")
        if r.get("tag") in ("smoke", "gym-eval") or r.get("project") == "agent-gym":
            continue          # routing checks and evals are not real delegated work
        if cid:
            by_id.setdefault(cid, []).append(r)
    stats = {}
    for cid, rs in by_id.items():
        tag = next((r.get("tag") for r in rs if r.get("tag")), "?")
        s = stats.setdefault(tag, {"runs": 0, "escalated": 0, "exhausted": 0,
                                   "first_rung_fail": {}, "reasons": {}})
        s["runs"] += 1
        # The "exhausted" row only marks that nothing won; it is not a rung that failed.
        fails = [r for r in rs if r.get("script") == "cascade-attempt"
                 and r.get("status") != "exhausted"]
        won = any(r.get("script") != "cascade-attempt" for r in rs)
        if fails:
            s["escalated"] += 1
            first = min(fails, key=lambda r: r.get("rung_index", 0))
            s["first_rung_fail"][first.get("rung")] = s["first_rung_fail"].get(first.get("rung"), 0) + 1
            for r in fails:
                k = r.get("gate_reason") or "?"
                s["reasons"][k] = s["reasons"].get(k, 0) + 1
        if not won:
            s["exhausted"] += 1
    return stats


def advice(stats, min_runs=5, threshold=0.4):
    """Plain-text suggestions. Prints only; it never edits a routing table, because a
    table changed by a report nobody read is the stale-derived-state bug class."""
    out = []
    for tag, s in sorted(stats.items()):
        if s["runs"] < min_runs:
            continue
        rate = s["escalated"] / s["runs"]
        if rate >= threshold:
            worst = max(s["first_rung_fail"], key=s["first_rung_fail"].get)
            out.append(f"{tag}: {rate:.0%} of {s['runs']} runs escalated off {worst} "
                       f"-> consider starting the ladder above {worst}.")
        if s["exhausted"]:
            out.append(f"{tag}: {s['exhausted']} run(s) failed every rung "
                       f"-> the prompt or the gate is the problem, not the model.")
    return out


def _ledger_path():
    return os.environ.get("SMITH_LEDGER") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "usage.jsonl")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true", help="escalation rates per tag")
    args = ap.parse_args()
    if not args.report:
        ap.print_help()
        return
    rows = []
    try:
        with open(_ledger_path()) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError as e:
        print(f"ERROR: cannot read ledger: {e}", file=sys.stderr)
        sys.exit(2)
    stats = summarize(rows)
    if not stats:
        print("No cascade runs in the ledger yet (use gemini.py --cascade).")
        return
    print(f"{'tag':<18}{'runs':>5}{'escalated':>10}{'exhausted':>10}  first-rung failures / reasons")
    for tag, s in sorted(stats.items()):
        print(f"{tag:<18}{s['runs']:>5}{s['escalated']:>10}{s['exhausted']:>10}  "
              f"{s['first_rung_fail'] or '-'} / {s['reasons'] or '-'}")
    for line in advice(stats):
        print("->", line)


if __name__ == "__main__":
    main()
