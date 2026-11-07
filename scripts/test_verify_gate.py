#!/usr/bin/env python3
"""Probes for the verify gate: smith_agent.py runs the contract's `verify` command ITSELF.

Found 2026-11-07 by a scripted-model probe. The contract's `verify:` was only an instruction to
the model ("do not call finish until it passes"). Nothing on the harness side ran it, so four runs
came back with one identical summary (finished True, stop finish): one that never ran verify, one
that ran only `ls`, one that RAN a failing verify and finished anyway, and one whose verify exited
0 having tested nothing. Claude's own review was the only backstop, and the summary gave it no
hint where to look.

Now, when the model calls finish, the harness runs the command in the worktree and reads the real
exit code. A failure sends the model back (twice at most); after that the run is accepted but
recorded as UNVERIFIED. verified is True / False / None (None: the run ended without a finish
attempt). A contract with no verify command gets no new keys at all.

Pure stdlib: the ollama `chat` backend is swapped for a scripted queue; no model, no network.

    python3 -B scripts/test_verify_gate.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("smith_agent", os.path.join(_HERE, "smith_agent.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


GITENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
              GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def make_repo():
    d = tempfile.mkdtemp(prefix="vg-repo-")
    for a in (["git", "init", "-q"], ["git", "add", "-A"]):
        pass
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, env=GITENV)
    os.makedirs(os.path.join(d, "src"))
    open(os.path.join(d, "src", "a.py"), "w").write("x = 0\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, env=GITENV)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True, env=GITENV)
    return d


# `grep` passes only in the worktree, only after the model's edit: the base repo says x = 0.
PASSES_WHEN_FIXED = 'grep -q "x = 1" src/a.py && echo "1 passed in 0.01s"'
ALWAYS_FAILS = 'echo "boom: 3 failed"; exit 1'
WRITE_RIGHT = ("write_file", {"path": "src/a.py", "content": "x = 1\n"})
WRITE_WRONG = ("write_file", {"path": "src/a.py", "content": "x = 2\n"})
FINISH = ("finish", {"summary": "done and verified"})


def drive(calls, verify, extra=(), env=None):
    """Run main() against a scripted queue. A None entry is a prose-only reply, no tool call.
    Returns (summary JSON, last ledger row, the messages the model saw on each call, ran_out)."""
    queue, seen, state = list(calls), [], {"ran_out": False}

    def fake_chat(model, messages, num_ctx, tools=True):
        seen.append([dict(m) for m in messages])
        if not queue:
            state["ran_out"] = True
            raise AssertionError("the scripted model ran out of calls")
        item = queue.pop(0)
        if item is None:
            return {"message": {"role": "assistant", "content": "all done, nothing more to do"}}
        name, args = item
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": name, "arguments": args}}]}}

    repo, wt = make_repo(), tempfile.mkdtemp(prefix="vg-wt-")
    contract = os.path.join(wt, "task.md")
    front = 'write_scope: ["src/**"]\n' + (f"verify: {verify}\n" if verify else "")
    open(contract, "w").write(f"---\n{front}---\nSet x to 1 in src/a.py.\n")
    saved = {k: os.environ.get(k) for k in ("SMITH_WORKTREES", "SMITH_LEDGER", "SMITH_VERIFY_GATE")}
    os.environ["SMITH_WORKTREES"], os.environ["SMITH_LEDGER"] = wt, os.path.join(wt, "usage.jsonl")
    os.environ.pop("SMITH_VERIFY_GATE", None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    old = sm.chat, sys.argv
    sm.chat, sys.argv = fake_chat, [sm.__name__, "--model", "fake", "--repo", repo,
                                    "--contract", contract, "--max-turns", "12", *extra]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                sm.main()
            except SystemExit:
                pass
    finally:
        sm.chat, sys.argv = old
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    summary = json.loads(buf.getvalue().strip().splitlines()[-1])
    rows = [json.loads(l) for l in open(os.path.join(wt, "usage.jsonl")) if l.strip()]
    return summary, [r for r in rows if r.get("script") == "smith_agent"][-1], seen, state["ran_out"]


# ---- the verify command passes ----------------------------------------------------------------
s, led, seen, out = drive([WRITE_RIGHT, FINISH], PASSES_WHEN_FIXED)
check(s["verified"] is True and s["verify"]["rc"] == 0 and s["verify"]["bounces"] == 0 and not out,
      "verify passes at the first finish: verified True, rc 0, no bounce")
check(s["finished"] is True and s["stop"] == "finish" and s["turns"] == 2,
      "...and the run itself is unchanged (finished, stop finish, 2 turns)")
check("1 passed" in s["verify"]["tail"], "the output tail is kept in the summary")

# ---- it runs in the WORKTREE, on the model's edit ---------------------------------------------
check(s["verified"] is True,
      "verify saw the model's edit (the base repo says x = 0, so only the worktree can pass)")

# ---- a failure sends the model back, and it can fix it ----------------------------------------
s, led, seen, out = drive([WRITE_WRONG, FINISH, WRITE_RIGHT, FINISH], PASSES_WHEN_FIXED)
check(s["verified"] is True and s["verify"]["bounces"] == 1 and s["turns"] == 4 and not out,
      "verify fails, the model is sent back, fixes it, finishes: verified True after 1 bounce")

# ---- what the model is told ---------------------------------------------------------------------
# The output must differ from the command's own text, or the refusal (which always shows the
# command) would satisfy the check even with the output dropped.
FAILS_WITH_OUTPUT = 'python3 -c "print(\'FAI\' + \'LED test_a: \' + str(1 + 1)); raise SystemExit(1)"'
s, led, seen, out = drive([WRITE_WRONG, FINISH, FINISH, FINISH], FAILS_WITH_OUTPUT)
refusal = seen[2][-1].get("content", "")        # the tool result the model saw after its first finish
check("FAILED test_a: 2" not in FAILS_WITH_OUTPUT, "(the probe's output is not spelled in its own command)")
check("finish REFUSED" in refusal and "FAILED test_a: 2" in refusal and "exit code: 1" in refusal,
      "the first refusal carries the command's exit code and its OUTPUT")
check("1 attempt(s) left" in refusal, "...and says how many attempts are left")
check(any("The harness runs this command itself" in str(m.get("content", "")) for m in seen[0]),
      "the task text tells the model the harness will run verify")

# ---- it never passes: accepted, but flagged ------------------------------------------------------
check(s["verified"] is False and s["verify"]["rc"] == 1 and s["verify"]["bounces"] == 2,
      "verify keeps failing: after 2 bounces the run is accepted, with verified False and rc 1")
check(s["finished"] is True and s["stop"] == "finish" and s["turns"] == 4,
      "...the existing keys keep their meaning (finished True, stop finish), so no consumer breaks")
check("FAILED test_a: 2" in s["verify"]["tail"], "...and the failing output is in the summary")

# ---- the probe's four cases, now told apart ----------------------------------------------------
s, *_ = drive([WRITE_RIGHT, FINISH, FINISH, FINISH], ALWAYS_FAILS)
check(s["verified"] is False, "A: never ran verify, finished at once -> verified False")
s, *_ = drive([WRITE_RIGHT, ("run_command", {"command": "ls"}), FINISH, FINISH, FINISH], ALWAYS_FAILS)
check(s["verified"] is False, "B: ran only `ls`, then finished -> verified False")
s, *_ = drive([WRITE_RIGHT, ("run_command", {"command": ALWAYS_FAILS}), FINISH, FINISH, FINISH], ALWAYS_FAILS)
check(s["verified"] is False and s["verify"]["rc"] == 1,
      "C: ran the failing verify, saw exit 1, finished anyway -> verified False, rc 1")
s, *_ = drive([WRITE_RIGHT, FINISH], 'python3 -c "pass"')
check(s["verified"] is True and s["verify"]["tail"] == "",
      "D: verify exits 0 having tested nothing -> verified True with an EMPTY tail, so the reviewer can see it")

# ---- a run that never reaches finish -------------------------------------------------------------
s, led, seen, out = drive([WRITE_RIGHT], PASSES_WHEN_FIXED, extra=["--max-turns", "1"])
check(s["verified"] is None and "verify" not in s and s["stop"] == "max_turns",
      "ran out of turns without a finish attempt: verified None, nothing run")

# ---- a prose give-up that still counts as finished ------------------------------------------------
s, led, seen, out = drive([WRITE_RIGHT, None, None], PASSES_WHEN_FIXED)
check(s["stop"] == "no_tools" and s["verified"] is True and s["verify"]["bounces"] == 0,
      "a prose-only finish is verified once at the end")
s, *_ = drive([WRITE_WRONG, None, None], PASSES_WHEN_FIXED)
check(s["stop"] == "no_tools" and s["verified"] is False, "...and flagged when it fails")

# ---- timeouts are failures, never passes ---------------------------------------------------------
old_t = sm.VERIFY_TIMEOUT
sm.VERIFY_TIMEOUT = 1
try:
    s, *_ = drive([WRITE_RIGHT, FINISH, FINISH, FINISH], "sleep 5")
finally:
    sm.VERIFY_TIMEOUT = old_t
check(s["verified"] is False and s["verify"]["rc"] is None and s["verify"]["timed_out"] is True,
      "a verify that times out: verified False, rc None, timed_out True")

# ---- the output tail is capped ----------------------------------------------------------------------
s, *_ = drive([WRITE_RIGHT, FINISH, FINISH, FINISH], 'python3 -c "print(\'x\' * 5000 + \'END\'); raise SystemExit(1)"')
check(len(s["verify"]["tail"]) <= sm.VERIFY_TAIL and s["verify"]["tail"].endswith("END"),
      "a long output is cut to the LAST characters, where the failure is")

# ---- the ledger row: one line per run ------------------------------------------------------------
s, led, *_ = drive([WRITE_WRONG, FINISH, FINISH, FINISH], ALWAYS_FAILS)
check(led.get("verified") is False and led.get("verify_rc") == 1 and led.get("verify_bounces") == 2
      and "verify" not in led, "the ledger row carries verified, verify_rc and verify_bounces, not the tail")

# ---- no contract verify: nothing changes ------------------------------------------------------------
s, led, *_ = drive([WRITE_RIGHT, FINISH], None)
check(all(k not in s for k in ("verified", "verify")) and all(k not in led for k in ("verified", "verify_rc")),
      "a contract with no verify command gets no new keys, in the summary or the ledger")

# ---- the switch --------------------------------------------------------------------------------------
s, led, seen, out = drive([WRITE_WRONG, FINISH], ALWAYS_FAILS, env={"SMITH_VERIFY_GATE": "0"})
check(all(k not in s for k in ("verified", "verify")) and s["finished"] is True and s["turns"] == 2,
      "SMITH_VERIFY_GATE=0: the harness does not run verify (the old behaviour)")
check(not any("The harness runs this command itself" in str(m.get("content", "")) for m in seen[0]),
      "...and the task text does not claim it will")

# ---- it composes with the older opt-in finish gate -----------------------------------------------------
s, led, seen, out = drive([WRITE_WRONG, FINISH, FINISH, WRITE_RIGHT, FINISH], PASSES_WHEN_FIXED,
                          extra=["--finish-gate"])
check(s["verified"] is True and s["verify"]["bounces"] == 1 and s["turns"] == 5 and not out,
      "--finish-gate bounces first, then the verify gate bounces, then a fixed finish passes")

# ---- the contract parser must hand the harness the command the author wrote -----------------------
def parse(line):
    p = os.path.join(tempfile.mkdtemp(), "c.md")
    open(p, "w").write(f"---\n{line}\n---\nbody\n")
    return sm.parse_contract(p)[0]


check(parse('verify: grep -q "x = 1" src/a.py && echo "1 passed"')["verify"]
      == 'grep -q "x = 1" src/a.py && echo "1 passed"',
      "a verify command that ENDS in a quote keeps it (it used to lose the closing quote)")
check(parse('verify: pytest -k "foo"')["verify"] == 'pytest -k "foo"', "...pytest -k \"foo\" is intact")
check(parse("verify: 'a' && 'b'")["verify"] == "'a' && 'b'",
      "...and two quoted words are not unwrapped into a'  &&  'b")
check(parse("model: 'glm-4.6'")["model"] == "glm-4.6" and parse("model: glm-4.6")["model"] == "glm-4.6"
      and parse('model: "glm-4.6"')["model"] == "glm-4.6",
      "a value wrapped in one quote pair is still unwrapped (single, double, none)")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
