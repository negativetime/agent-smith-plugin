#!/usr/bin/env python3
"""PreToolUse nudge: remind Claude that web research has a trusted fleet route.

The offload gap report has said the same thing since 2026-07-19 — web research is
~3% delegated while `research @ gemini-pro` sits at an 11-good/1-bad measured trust.
Documenting the habit in SKILL.md and memory did not change it, because the moment
of decision is when Claude reaches for WebSearch, not when it reads a doc.

Design constraints (Josh has ADHD — nudges must be sparse and actionable):
  * NEVER block. A blocked lookup is worse than an undelegated one.
  * Stay quiet for the first few searches in a session: one or two quick lookups
    are legitimately faster inline. A BURST is what signals real research.
  * Say the specific command, not "consider delegating".

Never allowed to break a tool call: any failure exits 0 silently.
"""
import json
import os
import sys
import time

# Quiet for this many WebSearch calls per session, then nudge on each Nth after.
QUIET_BEFORE = int(os.environ.get("SMITH_NUDGE_AFTER", "3"))
REPEAT_EVERY = max(1, int(os.environ.get("SMITH_NUDGE_EVERY", "4")))
STATE_DIR = "/tmp/agent-smith-nudge"


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("tool_name") != "WebSearch":
        return 0

    session = str(payload.get("session_id") or "nosession").replace("/", "_")[:80]
    n = 1
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fp = os.path.join(STATE_DIR, session + ".count")
        # Stale sessions shouldn't inherit an old count.
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

    query = ""
    try:
        query = str((payload.get("tool_input") or {}).get("query") or "")[:120]
    except Exception:
        pass

    msg = (
        f"[agent-smith] WebSearch #{n} this session. Grounded research has a measured "
        f"fleet route (research @ gemini-pro: 11 good / 1 bad) that costs no Claude "
        f"tokens:\n"
        f'  python3 ~/.claude/skills/agent-smith/scripts/gemini.py --search --tag research "{query}"\n'
        f"Output lands on stdout with grounding sources on stderr; verify load-bearing "
        f"claims before using them, then record verdict.py good|bad --tag research. "
        f"Ignore this if you need the answer inline right now — it is a nudge, not a rule."
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
