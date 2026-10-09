#!/usr/bin/env python3
"""Limit-hit handoff: keep working on z.ai GLM when Claude hits a usage limit, and
bring GLM's work back to Claude afterwards. One script, three Claude Code hooks
(it reads `hook_event_name` from the payload on stdin):

  StopFailure   Claude was cut off. Only a real subscription limit counts:
                error == "rate_limit" AND the message says "hit your session limit"
                or "hit your weekly limit" (fast-mode credit 429s share the type).
                Writes a handoff built locally from the transcript (zero tokens),
                writes ~/.claude/handoffs/resume-glm.sh (Lane A: --resume
                --fork-session on z.ai, measured 2026-09-12) and posts a notification.
                A SoundCheck session gets the handoff but NO resume script: the whole
                session would go to z.ai, and SoundCheck material is Listen's IP.
  Stop          Fires every turn. Exits at once unless the last assistant turn came
                from a non-Claude model, i.e. this IS the GLM fork; then it rewrites
                the handoff, so Claude can see what GLM did.
  SessionStart  Injects the newest handoff only when GLM wrote it, it is under
                7 days old, its transcript still exists, the new session's folder
                overlaps its project folder, and no earlier session consumed it.

Handoffs live in ~/.claude/handoffs/, never in the work folder (they hold chat text).
Idea from github.com/Glazzy95/claude-mammouth-handoff (MIT), reviewed 2026-10-09.

Manual use:  limit_handoff.py --build <transcript.jsonl>   print a handoff to stdout
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
DIR = os.environ.get("LIMIT_HANDOFF_DIR") or os.path.join(HOME, ".claude", "handoffs")
LATEST = os.path.join(DIR, "latest.json")
CONSUMED = os.path.join(DIR, "consumed.txt")
RESUME = os.path.join(DIR, "resume-glm.sh")
MAX_AGE_S = 7 * 86400
LIMIT_RE = re.compile(r"hit your (session|weekly) limit", re.I)
# SoundCheck = Listen, Inc.'s IP: never sent to a cloud backend. A session counts as SoundCheck
# when it USES the material (its folders, files, MCP tools, skills) or Josh names it in a prompt.
# Passing mentions in shell text do not count: 102 of 108 real limit hits were false positives
# under a bare word match (2026-10-09 corpus).
SC_WORD = re.compile(r"sound\s*check", re.I)
# A real path: the folder name right after a "/". Bare names (memory files such as
# soundcheck-notes.md) are mentions, not use.
SC_PATH = re.compile(r"/soundcheck[\w-]*(?=[/\s\"'`]|$)", re.I)


SC_TOOL = re.compile(r"^(mcp__soundcheck|mcp__plugin_soundcheck)", re.I)
# Lane A recipe, measured 2026-09-12 (Notesmith "Provider handoff via Agent Notes").
# `--model 'opus[1m]'` is load-bearing (2026-10-09): without it Claude Code gives glm-5.3 a
# 200k window and a long session fails "Prompt is too long" before any call; with it the
# 09-12 fork resumed at 156k input tokens. GLM's true ceiling past that is unmeasured.
ZAI_ENV = {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
    "API_TIMEOUT_MS": "600000",
}


def shorten(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " ...[cut]"


def is_claude(model):
    return bool(model) and model.startswith("claude")


def entries(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def last_model(path, tail=512 * 1024):
    """Model of the newest assistant turn, reading only the file's tail."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - tail))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "assistant" and not d.get("isSidechain"):
            m = d.get("message", {}).get("model")
            if m and m != "<synthetic>":
                return m
    return None


def human_text(d):
    """A prompt the person typed, or None for tool results, notifications, meta."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
        return None
    origin = d.get("origin") or {}
    if origin.get("kind") not in (None, "human") or d.get("promptSource") == "system":
        return None
    c = d.get("message", {}).get("content")
    blocks = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if b.get("type") == "text"]
    keep = [b.strip() for b in blocks
            if b.strip() and (not b.lstrip().startswith("<") or b.lstrip().startswith("<pasted_content"))]
    return "\n".join(keep) or None


def describe(block):
    i = block.get("input") or {}
    name = block.get("name", "?")
    if name in ("Edit", "Write", "NotebookEdit", "Read"):
        return f"{name} {i.get('file_path') or i.get('notebook_path') or ''}".strip()
    if name == "Bash":
        return "Bash: " + (i.get("description") or shorten(i.get("command"), 150))
    return name


def uses_soundcheck(block):
    name, i = block.get("name", ""), block.get("input") or {}
    if SC_TOOL.search(name):
        return True
    if name == "Skill" and SC_WORD.search(str(i.get("skill", ""))):
        return True
    if name in ("Agent", "Task") and SC_WORD.search(f"{i.get('description', '')} {i.get('prompt', '')}"):
        return True  # the subagent's report lands in this transcript
    fields = [i.get(k) for k in ("file_path", "notebook_path", "path", "command", "pattern")]
    return any(isinstance(v, str) and SC_PATH.search(v) for v in fields)


def scan(path):
    first, recent, actions, files = [], [], [], []
    last_reply, models, cwd, sc_hit = "", [], None, False
    for d in entries(path):
        cwd = d.get("cwd") or cwd
        t = human_text(d)
        if t:
            if len(first) < 3:
                first.append(t)
            recent.append(t)
            last_reply = ""
            sc_hit = sc_hit or bool(SC_WORD.search(t))
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        msg = d.get("message", {})
        if msg.get("model") and msg["model"] != "<synthetic>":
            models.append(msg["model"])
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                last_reply = b["text"]
            elif b.get("type") == "tool_use":
                actions.append(describe(b))
                sc_hit = sc_hit or uses_soundcheck(b)
                if b.get("name") in ("Edit", "Write", "NotebookEdit"):
                    p = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                    if p and (p, msg.get("model")) not in files:
                        files.append((p, msg.get("model")))
    sc_hit = sc_hit or bool(SC_PATH.search(cwd or ""))
    return dict(first=first, recent=recent[-5:], actions=actions[-12:], files=files,
                last_reply=last_reply, model=models[-1] if models else None, cwd=cwd, soundcheck=sc_hit)


def render(s, session_id, status, origin_session=None):
    who = s["model"] or "unknown model"
    agent = "Claude" if is_claude(s["model"]) else who
    out = ["# HANDOFF - continue this work", "",
           f"Written by: **{who}** via Claude Code (limit_handoff hook), "
           f"{datetime.now():%Y-%m-%d %H:%M}. Status: **{status}**.",
           f"Project folder: `{s['cwd']}`", f"Session: `{session_id}`"]
    if origin_session:
        out.append(f"Forked from Claude session: `{origin_session}`")
    out += ["", "## How to continue",
            "- The last user request below is the current task. Check its state in the files before changing anything.",
            "- If the last reply ends mid-task or the status says STOPPED, finish that step first.",
            "- Follow CLAUDE.md in the project.", "", "## How the session started"]
    for p in s["first"]:
        out += ["> " + shorten(p, 1200).replace("\n", "\n> "), ""]
    out.append("## Latest user requests (oldest first, last one = current task)")
    out += [f"{n}. " + shorten(p, 1500).replace("\n", "\n   ") for n, p in enumerate(s["recent"], 1)]
    out += ["", f"## {agent}'s last reply",
            shorten(s["last_reply"], 5000) if s["last_reply"] else "_(none after the last request: still working when it stopped)_",
            "", "## Last actions (most recent last)"]
    out += [f"- {a}" for a in s["actions"]] or ["_(none)_"]
    mine = [f for f, m in s["files"] if m == s["model"]]
    others = [f for f, m in s["files"] if m != s["model"] and f not in mine]
    out += ["", f"## Files {agent} changed"]
    out += [f"- `{f}`" for f in mine] or ["_(none)_"]
    if others:
        out += ["", "## Files changed earlier in the session (other models)"]
        out += [f"- `{f}`" for f in dict.fromkeys(others)]
    return "\n".join(out) + "\n"


def write_handoff(s, session_id, transcript, status, origin_session=None):
    os.makedirs(DIR, exist_ok=True)
    md = os.path.join(DIR, f"{session_id}.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(render(s, session_id, status, origin_session))
    meta = dict(session_id=session_id, transcript=transcript, md=md, model=s["model"], cwd=s["cwd"],
                written_at=time.time(), origin_session=origin_session, soundcheck=s["soundcheck"])
    tmp = LATEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, LATEST)
    return md


def notify(text):
    if os.environ.get("LIMIT_HANDOFF_NO_NOTIFY"):
        return
    try:
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"Claude limit hit\""],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def resume_script(session_id, cwd):
    env = " \\\n  ".join(f"{k}={shlex.quote(v)}" for k, v in ZAI_ENV.items())
    return f"""#!/bin/zsh
# Continue Claude session {session_id} on z.ai GLM (Lane A). Written by limit_handoff.py.
# The key stays in your shell env, never in this file.
[[ -z "$ZAI_API_KEY" ]] && ZAI_API_KEY=$(zsh -ic 'print -r -- $ZAI_API_KEY' 2>/dev/null)
[[ -z "$ZAI_API_KEY" ]] && {{ echo "ZAI_API_KEY is not set"; exit 1; }}
unset ANTHROPIC_API_KEY
cd {shlex.quote(cwd or HOME)} || exit 1
export LIMIT_HANDOFF_ORIGIN={shlex.quote(session_id)}
exec env ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY" \\
  {env} \\
  claude --model 'opus[1m]' --resume {shlex.quote(session_id)} --fork-session
"""


def on_stop_failure(p):
    if p.get("error") != "rate_limit" or not LIMIT_RE.search(p.get("last_assistant_message") or ""):
        return
    tr = p.get("transcript_path")
    if not tr or not os.path.exists(tr):
        return
    s = scan(tr)
    s["cwd"] = p.get("cwd") or s["cwd"]
    s["soundcheck"] = s["soundcheck"] or bool(SC_PATH.search(s["cwd"] or ""))
    sid = p.get("session_id")
    md = write_handoff(s, sid, tr, "STOPPED: " + shorten(p.get("last_assistant_message"), 120))
    if s["soundcheck"]:
        if os.path.exists(RESUME):
            os.remove(RESUME)  # an older session's script must not look like this one's
        notify("SoundCheck session: not offered on z.ai. Wait for the reset or use the local fleet.")
        return
    with open(RESUME, "w") as f:
        f.write(resume_script(sid, s["cwd"]))
    os.chmod(RESUME, 0o700)
    notify(f"Continue on GLM: run {RESUME.replace(HOME, '~')}")


def on_stop(p):
    tr = p.get("transcript_path")
    if not tr or not os.path.exists(tr):
        return
    model = last_model(tr)
    if not model or is_claude(model):
        return
    s = scan(tr)
    write_handoff(s, p.get("session_id"), tr, f"{model} finished its last reply",
                  origin_session=os.environ.get("LIMIT_HANDOFF_ORIGIN"))


def overlaps(a, b):
    if not a or not b:
        return False
    a, b = os.path.realpath(a).rstrip("/") + "/", os.path.realpath(b).rstrip("/") + "/"
    return a.startswith(b) or b.startswith(a)


def on_session_start(p):
    if p.get("source") == "compact" or not os.path.exists(LATEST):
        return
    try:
        meta = json.load(open(LATEST))
    except ValueError:
        return
    sid = p.get("session_id")
    if is_claude(meta.get("model")) or not meta.get("model"):
        return
    if sid == meta.get("session_id") or time.time() - meta.get("written_at", 0) > MAX_AGE_S:
        return
    # Stale-derived-state rule: the record is not the artifact. Both files must still exist.
    if not os.path.exists(meta.get("md", "")) or not os.path.exists(meta.get("transcript", "")):
        return
    if not overlaps(p.get("cwd"), meta.get("cwd")):
        return
    key = f"{meta['session_id']}@{meta['written_at']}"
    if os.path.exists(CONSUMED) and key in open(CONSUMED).read().split():
        return
    text = open(meta["md"], encoding="utf-8").read()
    ctx = (f"While Claude was at its usage limit, {meta['model']} (z.ai) continued this work in Claude Code "
           f"session {meta['session_id']}. Its handoff is below. Treat its claims as unverified: check the "
           "files and any test results before building on them, then briefly tell Josh you picked up "
           f"{meta['model']}'s work.\n\n" + text)
    with open(CONSUMED, "a") as f:
        f.write(key + "\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        sys.stdout.write(render(scan(sys.argv[2]), "manual", "manual build"))
        return
    try:
        p = json.load(sys.stdin)
    except ValueError:
        return
    {"StopFailure": on_stop_failure, "Stop": on_stop, "SessionStart": on_session_start}.get(
        p.get("hook_event_name"), lambda _: None)(p)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a hook must never break a session
        try:
            os.makedirs(DIR, exist_ok=True)
            with open(os.path.join(DIR, "errors.log"), "a") as f:
                f.write(f"{datetime.now().isoformat()} {type(e).__name__}: {e}\n")
        except Exception:
            pass
    sys.exit(0)
