#!/usr/bin/env python3
"""Test for limit_handoff.py on synthetic transcripts (pure stdlib, no model calls).

Runs the hook as a subprocess, the way Claude Code does, against a temp handoff
folder. Covers: which StopFailure payloads trigger, the SoundCheck refusal, the
Stop hook's Claude/GLM split, and every SessionStart guard.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "limit_handoff.py")
T = tempfile.mkdtemp(prefix="limit_handoff_test_")
D = os.path.join(T, "handoffs")
ENV = {**os.environ, "LIMIT_HANDOFF_DIR": D, "LIMIT_HANDOFF_NO_NOTIFY": "1"}
WORK = os.path.join(T, "proj")
os.makedirs(WORK)
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def run(payload, env=ENV):
    r = subprocess.run([sys.executable, "-B", HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def transcript(name, turns, cwd=WORK):
    """turns: ("user", text) | ("compact", text) | (model, text, [tool_use dicts])."""
    path = os.path.join(T, name + ".jsonl")
    with open(path, "w") as f:
        for t in turns:
            if t[0] == "user":
                d = {"type": "user", "cwd": cwd, "origin": {"kind": "human"},
                     "message": {"role": "user", "content": [{"type": "text", "text": t[1]}]}}
            elif t[0] == "compact":
                d = {"type": "user", "cwd": cwd, "isCompactSummary": True,
                     "message": {"role": "user", "content": t[1]}}
            else:
                content = [{"type": "text", "text": t[1]}] + [dict(type="tool_use", **u) for u in t[2]]
                d = {"type": "assistant", "cwd": cwd, "message": {"model": t[0], "content": content}}
            f.write(json.dumps(d) + "\n")
    return path


def limit(tr, sid, msg="You've hit your session limit · resets 1pm (America/New_York)", error="rate_limit"):
    return {"hook_event_name": "StopFailure", "session_id": sid, "transcript_path": tr, "cwd": WORK,
            "error": error, "last_assistant_message": msg}


edit = {"name": "Edit", "input": {"file_path": WORK + "/a.py"}}
claude_tr = transcript("claude", [
    ("user", "fix the parser"),
    ("compact", "This session is being continued... SoundCheck material never goes to cloud backends."),
    ("claude-opus-5", "on it", [edit, {"name": "Bash", "input": {"command": "grep -n x soundcheck-network-audit.md",
                                                                  "description": "Search memory"}}]),
])
sc_tr = transcript("sc", [
    ("user", "update the bench script"),
    ("claude-opus-5", "reading", [{"name": "Read", "input": {"file_path": "/Users/j/Python/soundcheck-mac-automation/x.py"}}]),
])
sc_skill_tr = transcript("sc_skill", [
    ("user", "file this bug"),
    ("claude-opus-5", "ok", [{"name": "Skill", "input": {"skill": "soundcheck-jira-bug"}}]),
])
glm_tr = transcript("glm", [
    ("user", "fix the parser"),
    ("claude-opus-5", "on it", [edit]),
    ("user", "continue"),
    ("glm-5.3", "done", [{"name": "Write", "input": {"file_path": WORK + "/b.py"}}]),
])

# StopFailure: only a real subscription limit triggers.
R = os.path.join(D, "resume-glm.sh")
run(limit(claude_tr, "s1"))
check("session limit -> handoff", os.path.exists(os.path.join(D, "s1.md")))
check("session limit -> resume script", os.path.exists(R))
script = open(R).read()
check("resume script forks the right session", "--resume s1 --fork-session" in script)
check("resume script carries no key", "ZAI_API_KEY=" not in script.replace('ZAI_API_KEY=$(', ""))
check("compaction summary is not a mention of SoundCheck", "SoundCheck" not in open(os.path.join(D, "s1.md")).read())
check("memory filename is not SoundCheck use", not json.load(open(os.path.join(D, "latest.json")))["soundcheck"])
before = sorted(os.listdir(D))
run(limit(claude_tr, "s2", msg="Usage credits are required for fast mode."))
run(limit(claude_tr, "s3", error="server_error", msg="hit your session limit"))
check("fast-mode 429 and other errors ignored", sorted(os.listdir(D)) == before)
run(limit(claude_tr, "s4", msg="You've hit your weekly limit · resets Oct 3 at 4am"))
check("weekly limit triggers", os.path.exists(os.path.join(D, "s4.md")))

# SoundCheck: handoff yes, resume script no, and an older one is removed.
run(limit(sc_tr, "sc1"))
check("SoundCheck file -> no resume script", not os.path.exists(R))
run(limit(claude_tr, "s5"))
run(limit(sc_skill_tr, "sc2"))
check("SoundCheck skill -> no resume script", not os.path.exists(R))
check("SoundCheck handoff still written locally", os.path.exists(os.path.join(D, "sc2.md")))

# Stop: Claude turns write nothing; a GLM turn writes a GLM handoff.
meta = open(os.path.join(D, "latest.json")).read()
run({"hook_event_name": "Stop", "session_id": "s1", "transcript_path": claude_tr, "cwd": WORK})
check("Stop on a Claude turn writes nothing", open(os.path.join(D, "latest.json")).read() == meta)
genv = dict(ENV, LIMIT_HANDOFF_ORIGIN="s1")
run({"hook_event_name": "Stop", "session_id": "g1", "transcript_path": glm_tr, "cwd": WORK}, genv)
md = open(os.path.join(D, "g1.md")).read()
check("GLM handoff: written by glm-5.3", "Written by: **glm-5.3**" in md)
check("GLM handoff: origin session", "Forked from Claude session: `s1`" in md)
mine = md.split("## Files glm-5.3 changed")[1].split("##")[0]
check("GLM handoff: lists GLM's files, not Claude's", "b.py" in mine and "a.py" not in mine)


def start(sid="c1", cwd=WORK, source="startup"):
    return run({"hook_event_name": "SessionStart", "session_id": sid, "cwd": cwd, "source": source})


check("SessionStart compact -> nothing", start(source="compact") == "")
check("SessionStart unrelated folder -> nothing", start(cwd=T + "/elsewhere") == "")
check("SessionStart in the GLM session itself -> nothing", start(sid="g1") == "")
out = start(cwd=WORK + "/sub")
check("SessionStart in a subfolder -> injects", "glm-5.3" in out and "unverified" in out)
check("consumed handoff is not injected twice", start(sid="c2") == "")
run({"hook_event_name": "Stop", "session_id": "g1", "transcript_path": glm_tr, "cwd": WORK}, genv)
m = json.load(open(os.path.join(D, "latest.json")))
m["written_at"] -= 8 * 86400
json.dump(m, open(os.path.join(D, "latest.json"), "w"))
check("handoff over 7 days old -> nothing", start(sid="c3") == "")
run({"hook_event_name": "Stop", "session_id": "g1", "transcript_path": glm_tr, "cwd": WORK}, genv)
os.remove(json.load(open(os.path.join(D, "latest.json")))["md"])
check("handoff file deleted -> nothing", start(sid="c4") == "")
run(limit(claude_tr, "s6"))
check("Claude-written handoff is never injected", start(sid="c5") == "")
check("no errors logged", not os.path.exists(os.path.join(D, "errors.log")))

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
