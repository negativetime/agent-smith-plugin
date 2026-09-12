#!/usr/bin/env python3
"""Lifecycle probes for smith_agent.py --repo (scoped worktree drafting).

Pure stdlib, no model calls: the ollama `chat` backend is swapped for a scripted queue of
tool calls, so the REAL loop, the REAL preflight and the REAL settlement all run.

These are lifecycle probes ("model does X, then Y; what must be true?"), not a compile
check. P3, P5 and P6 are the three a naive implementation passes silently:
  P3  the model writes out of scope via run_command, never touching write_file
  P5  the model commits in the worktree, so worktree HEAD no longer shows the change
  P6  the model changes nothing and calls finish
"""
import importlib.util
import io
import json
import os
import contextlib
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("smith_agent",
                                              os.path.join(_HERE, "smith_agent.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

all_pass = True


def check(ok, what):
    global all_pass
    print(("PASS  " if ok else "FAIL  ") + what)
    all_pass = all_pass and bool(ok)


GITENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
              GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def sh(cwd, *args):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=GITENV)
    assert p.returncode == 0, f"{args}: {p.stderr}"
    return p.stdout


def make_repo():
    """A repo with src/a.py (in scope), README.md (out of scope) and an ignored asset."""
    d = tempfile.mkdtemp(prefix="smith-repo-")
    os.makedirs(os.path.join(d, "src"))
    open(os.path.join(d, "src", "a.py"), "w").write("VALUE = 1\n")
    open(os.path.join(d, "README.md"), "w").write("# readme\n")
    open(os.path.join(d, ".gitignore"), "w").write("assets/\n")
    sh(d, "git", "init", "-q")
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "base")
    os.makedirs(os.path.join(d, "assets"))
    open(os.path.join(d, "assets", "big.bin"), "w").write("payload")
    return d


def run(calls, repo, extra=(), chat=None):
    """Drive main() with a scripted tool-call queue. Returns the summary JSON."""
    queue = list(calls)

    def fake_chat(model, messages, num_ctx, tools=True):
        name, args = queue.pop(0)
        return {"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": name, "arguments": args}}]}}

    wtroot = tempfile.mkdtemp(prefix="smith-wt-")
    prompt = os.path.join(wtroot, "task.txt")
    open(prompt, "w").write("do the thing")
    argv = [sm.__name__, "--model", "fake", "--repo", repo, "--prompt-file", prompt,
            "--max-turns", "12", *extra]
    old_chat, old_argv = sm.chat, sys.argv
    os.environ["SMITH_WORKTREES"] = wtroot
    os.environ["SMITH_LEDGER"] = os.path.join(wtroot, "usage.jsonl")
    sm.chat, sys.argv = (chat or fake_chat), argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                sm.main()
            except SystemExit:
                pass
    finally:
        sm.chat, sys.argv = old_chat, old_argv
    return json.loads(buf.getvalue().strip().splitlines()[-1])


IN, OUT = "src/a.py", "README.md"
SCOPE = ["--write-scope", "src/**"]

# ---------------------------------------------------------------- unit: globs
check(sm.glob_to_re("src/**").match("src/deep/x.py") is not None, "glob: ** spans separators")
check(sm.glob_to_re("src/*.py").match("src/deep/x.py") is None, "glob: * stops at a separator")
check(sm.glob_to_re("src/**.swift").match("src/E/F.swift") is not None, "glob: **.swift nests")
check(sm.glob_to_re("a/**/b").match("a/b") is not None, "glob: a/**/b also matches a/b")
for bad in ("**", "*", "/abs", "../x", ".git/**"):
    try:
        sm.validate_scope([bad], "write")
        check(False, f"validate_scope rejects {bad!r}")
    except sm.RepoError:
        check(True, f"validate_scope rejects {bad!r}")
try:
    sm.validate_scope([], "write")
    check(False, "validate_scope rejects an empty scope")
except sm.RepoError:
    check(True, "validate_scope rejects an empty scope")

# DENY_RE must not fire on innocent words that merely contain the token
import re as _re
check(not any(_re.search(p, "echo walked through the door") for p in sm.DENY_RE),
      "DENY_RE: 'through' does not trip the gh rule")
check(any(_re.search(p, "git push origin main") for p in sm.DENY_RE), "DENY_RE: git push blocked")
check(any(_re.search(p, "gh pr create") for p in sm.DENY_RE), "DENY_RE: gh blocked")
check(not any(_re.search(p, "git status --short") for p in sm.DENY_RE), "DENY_RE: git status allowed")

# ---------------------------------------------------------------- P1 in-scope edit
repo = make_repo()
s1 = run([("write_file", {"path": IN, "content": "VALUE = 2\n"}),
          ("finish", {"summary": "done"})], repo, SCOPE)
check(s1["settled"] and s1["finished"], "P1 in-scope edit settles")
check(s1["files"] == ["M\tsrc/a.py"], f"P1 diff is exactly the edited file ({s1['files']})")
check(s1["patch"] and os.path.exists(s1["patch"]), "P1 patch written")
ap = subprocess.run(["git", "-C", repo, "apply", "--check", s1["patch"]],
                    capture_output=True, text=True)
check(ap.returncode == 0, f"P1 patch applies to a clean base ({ap.stderr.strip()})")
check(not os.path.exists(s1.get("worktree") or "/nonexistent"), "P1 worktree removed on pass")

# ---------------------------------------------------------------- P2 advisory refusal
repo = make_repo()
s2 = run([("write_file", {"path": OUT, "content": "nope\n"}),
          ("write_file", {"path": IN, "content": "VALUE = 3\n"}),
          ("finish", {"summary": "done"})], repo, SCOPE)
check(s2["settled"], "P2 run continues after an out-of-scope write_file")
check(s2["files"] == ["M\tsrc/a.py"], f"P2 the refused write never landed ({s2['files']})")

# ---------------------------------------------------------------- P3 shell bypass
repo = make_repo()
s3 = run([("run_command", {"command": "echo pwned >> README.md"}),
          ("write_file", {"path": IN, "content": "VALUE = 4\n"}),
          ("finish", {"summary": "done"})], repo, SCOPE)
check(not s3["settled"] and s3["stop"] == "scope_violation",
      f"P3 run_command bypass caught at settle ({s3['stop']})")
check([v["path"] for v in s3["violations"]] == [OUT], f"P3 names README.md ({s3['violations']})")
check(os.path.isdir(s3.get("worktree", "")), "P3 worktree kept for inspection")
check(os.path.exists(s3["worktree"] + ".violations.json"), "P3 violations.json written")

# ---------------------------------------------------------------- P4 deletion
repo = make_repo()
s4 = run([("run_command", {"command": "rm src/a.py"}),
          ("finish", {"summary": "done"})], repo, SCOPE)
check(not s4["settled"] and s4["stop"] == "scope_violation", "P4 in-scope deletion refused")
check("deletion" in (s4["violations"] or [{}])[0].get("why", ""),
      f"P4 refused BECAUSE it deletes ({s4['violations']})")
repo = make_repo()
s4b = run([("run_command", {"command": "rm src/a.py"}),
           ("finish", {"summary": "done"})], repo, SCOPE + ["--allow-delete"])
check(s4b["settled"] and s4b["files"] == ["D\tsrc/a.py"], "P4 --allow-delete permits it")

# ---------------------------------------------------------------- P5 model commits
repo = make_repo()
s5 = run([("write_file", {"path": IN, "content": "VALUE = 5\n"}),
          ("run_command", {"command": "git add -A && git -c user.name=m "
                                      "-c user.email=m@m commit -qm inner"}),
          ("finish", {"summary": "done"})], repo, SCOPE)
check(s5["settled"], "P5 settles after the model committed")
check(s5["files"] == ["M\tsrc/a.py"], f"P5 diff vs BASE still sees the change ({s5['files']})")

# ---------------------------------------------------------------- P6 no-op
repo = make_repo()
s6 = run([("run_command", {"command": "ls"}), ("finish", {"summary": "all good"})],
         repo, SCOPE)
check(not s6["settled"] and s6["stop"] == "finish_no_diff",
      f"P6 a finish that changed nothing is not a pass ({s6['stop']})")

# ---------------------------------------------------------------- P7 leftover worktree
repo = make_repo()
stale = tempfile.mkdtemp(prefix="smith-stale-")
try:
    sm.preflight_repo(repo, stale)
    check(False, "P7 preflight refuses a leftover worktree path")
except sm.RepoError as exc:
    check("worktree remove --force" in str(exc), "P7 preflight refuses and prints cleanup")

# ---------------------------------------------------------------- P8 carried assets
repo = make_repo()
before = sh(repo, "ls", "-la", os.path.join(repo, "assets"))
s8 = run([("write_file", {"path": IN, "content": "VALUE = 8\n"}),
          ("finish", {"summary": "done"})], repo,
         SCOPE + ["--carry-ignored", "assets"])
after = sh(repo, "ls", "-la", os.path.join(repo, "assets"))
check(s8["settled"] and "teardown_warning" not in s8, "P8 carried-asset run settles")
check(before == after, "P8 SOURCE assets intact across worktree removal")
check(open(os.path.join(repo, "assets", "big.bin")).read() == "payload",
      "P8 source asset is a real file, not a broken link")

# ---------------------------------------------------------------- .git deny
repo = make_repo()
wt = tempfile.mkdtemp(prefix="smith-git-")
sm.WRITE_SCOPE = None
check("not allowed" in sm.t_write_file(wt, ".git/config", "x"), ".git writes refused")
check("not allowed" in sm.t_write_file(wt, ".git", "x"), ".git pointer file refused")

# ---------------------------------------------------------------- arg validation
r = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "smith_agent.py"),
                    "--model", "m", "--repo", ".", "--prompt-file", os.devnull],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(r.returncode == 2 and "write-scope" in r.stderr, "repo mode without a write scope refuses")
r = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "smith_agent.py"),
                    "--model", "m", "--repo", ".", "--workdir", ".",
                    "--prompt-file", os.devnull],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(r.returncode == 2 and "exactly one" in r.stderr, "--repo and --workdir are exclusive")

# ---------------------------------------------------------------- P9 loop error not masked
# A backend failure that leaves no diff must NOT be relabelled "finish_no_diff": that reads
# as a clean no-op run. Found end-to-end, when a 404 came back looking like a quiet pass.
repo = make_repo()


def _boom(*a, **k):
    raise sm.urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b"nope"))


s9 = run([], repo, SCOPE, chat=_boom)
check(s9["stop"].startswith("error:http-404"), f"P9 backend error survives settle ({s9['stop']})")
check(s9["loop_stop"].startswith("error:"), "P9 loop_stop recorded")
check(not s9["settled"], "P9 an errored run does not settle")

# ------------------------------------------------- P10 verify-command build artifacts
# Found END-TO-END, not by the probes: the task told the model to run pytest, pytest wrote
# __pycache__, and the scope gate failed an otherwise correct run. Reported, not hidden.
repo = make_repo()
s10 = run([("write_file", {"path": IN, "content": "VALUE = 10\n"}),
           ("run_command", {"command": "mkdir -p __pycache__ && touch __pycache__/x.pyc "
                                       "src/y.pyc .DS_Store"}),
           ("finish", {"summary": "done"})], repo, SCOPE)
check(s10["settled"], f"P10 build artifacts do not fail a clean run ({s10['stop']})")
check(s10["files"] == ["M\tsrc/a.py"], f"P10 artifacts stay out of the patch ({s10['files']})")
check(sorted(s10.get("artifacts", [])) == ["\u002e\u0044S_Store".replace("\u002e","."),
                                           "__pycache__/x.pyc", "src/y.pyc"],
      f"P10 artifacts are REPORTED, not silently dropped ({s10.get('artifacts')})")

sys.exit(0 if all_pass else 1)
