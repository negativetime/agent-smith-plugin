#!/usr/bin/env python3
"""PreToolUse nudge: remind Claude that read-only subagent fan-out has a trusted fleet route.

gap_report.py has flagged this the same way since 2026-07-27 — read-only subagent
fan-out (Agent tool calls that just read/search/summarize, no mutation) is 0%
delegated against 735 Claude-side calls (~6M out tokens), while both
`subagent-fanout @ gpt-oss:20b` (local, free) and `subagent-fanout @ glm-5.2`
(z.ai Coding Plan, $18/mo flat, marginal cost $0) are measured good/0 bad.
Same lesson as research_nudge.py: documenting the gap in memory/SKILL.md didn't
move it, because the decision moment is when Claude reaches for the Agent tool,
not when it reads a doc.

Design constraints (Josh has ADHD — nudges must be sparse and actionable):
  * NEVER block. A blocked fan-out is worse than an undelegated one.
  * Quiet for the first couple Agent calls in a session — one-off dispatches are
    fine inline. A BURST (multiple independent read-only agents) is the signal.
  * Skip calls that aren't read-only fan-out: isolation:"worktree" implies file
    mutation, and specialized non-general agent types (code-reviewer, Plan, etc.)
    are doing judgment work the fleet hasn't earned trust on yet — only nudge on
    Explore / general-purpose, the two types the gap report's ledger evidence
    (gpt-oss:20b, glm-5.2) actually covers.
  * Say the specific command, not "consider delegating".

Never allowed to break a tool call: any failure exits 0 silently.
"""
import json
import os
import sys
import time

QUIET_BEFORE = int(os.environ.get("SMITH_FANOUT_NUDGE_AFTER", "2"))
REPEAT_EVERY = max(1, int(os.environ.get("SMITH_FANOUT_NUDGE_EVERY", "3")))
STATE_DIR = "/tmp/agent-smith-nudge"
ELIGIBLE_TYPES = {"Explore", "general-purpose", ""}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "Agent":
        return 0

    tool_input = payload.get("tool_input") or {}
    if tool_input.get("isolation"):
        return 0
    subagent_type = str(tool_input.get("subagent_type") or "")
    if subagent_type not in ELIGIBLE_TYPES:
        return 0

    session = str(payload.get("session_id") or "nosession").replace("/", "_")[:80]
    n = 1
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fp = os.path.join(STATE_DIR, session + ".fanout_count")
        if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) > 86400:
            os.remove(fp)
        if os.path.isfile(fp):
            with open(fp) as fh:
                n = int((fh.read() or "0").strip() or 0) + 1
        with open(fp, "w") as fh:
            fh.write(str(n))
    except Exception:
        pass

    if n < QUIET_BEFORE or (n - QUIET_BEFORE) % REPEAT_EVERY != 0:
        return 0

    description = ""
    try:
        description = str(tool_input.get("description") or "")[:120]
    except Exception:
        pass

    msg = (
        f"[agent-smith] Agent dispatch #{n} this session ({description or 'read-only fan-out'}). "
        f"If this is read-only (search/summarize/digest, no edits), it has a measured fleet route "
        f"that costs no Claude tokens:\n"
        f"  python3 ~/.claude/skills/agent-smith/scripts/gemini.py --tag subagent-fanout \"<prompt>\"\n"
        f"  (free local gpt-oss:20b by default; or --model glm-5.2 --base-url zai-coding for the paid "
        f"flat-rate lane)\n"
        f"For several similar lookups at once, use --batch. Verify load-bearing output, then "
        f"verdict.py good|bad --tag subagent-fanout. Ignore this if the task needs Claude's judgment "
        f"or full tool access — it's a nudge, not a rule."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "agent-smith delegation nudge (non-blocking)",
            "additionalContext": msg,
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
