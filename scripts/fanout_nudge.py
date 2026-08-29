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

# An explicit read-only DECLARATION outranks everything below it. Measured the
# hard way (twice in one hour, 2026-08-10): keyword matching cannot see negation.
# "Read-only investigation. Do NOT submit, release, or commit" was silenced by the
# `commit` marker, and "Do not modify any files" by `modify` — prohibitions read as
# intent. Any keyword rule over free text needs this precedence, not more keywords.
DECLARED_READONLY = (
    "read-only", "read only", "do not modify", "don't modify", "do not edit",
    "don't edit", "no edits", "without modifying", "do not write", "don't write",
    "do not change", "don't change", "make no changes",
)
# Any hint of mutation kills the nudge outright — false silence costs one
# undelegated read, false noise costs the credibility of every future nudge.
MUTATING_MARKERS = (
    "implement", "fix the", "fix this", "fix a ", "write the", "write a ",
    "create the", "create a ", "edit ", "refactor", "migrate", "port the",
    "apply the", "patch ", "rename", "delete ", "remove the",
    "add a ", "add the", "build the", "wire up", "make the change",
    "you are implementing", "modify the", "commit the",
)
# …and it must positively look like reading, not merely lack write-words.
READONLY_MARKERS = (
    "read-only", "read only", "do not modify", "don't modify", "summar",
    "find all", "search for", "locate", "investigate", "analyz", "audit",
    "which files", "where is", "identify", "list all", "trace", "map the",
    "look up", "research", "survey", "enumerate", "compare", "gather",
    "extract", "digest", "look for", "vet ",
)


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

    # MEASURED 2026-08-10: this hook fired 81/81 times and was ignored 81/81 times
    # — and ignoring it was mostly CORRECT. `subagent-driven-development` spawns
    # `general-purpose` agents to WRITE code ("Implement Task 2: …"), which looked
    # identical here to a read-only fan-out. Of 348 dispatches that passed the
    # checks above, ~225 were mutating; the real read-only gap is ~100, not 482.
    # Nudging an implementation dispatch to a model with no repo access is bad
    # advice, and bad advice at the decision moment trains the reader to skim past
    # the good advice too. So: read the prompt, and stay quiet unless it's read-only.
    text = " ".join(str(tool_input.get(k) or "") for k in ("description", "prompt"))
    low = text.lower()
    if not any(w in low for w in DECLARED_READONLY):
        # No explicit declaration, so fall back to reading the verbs.
        if any(w in low for w in MUTATING_MARKERS):
            return 0
        if not any(w in low for w in READONLY_MARKERS):
            return 0  # silence is the default, not the exception

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
