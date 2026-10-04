#!/usr/bin/env python3
"""Probes for smith_agent.py --explore and --fanout (read-only fleet investigation).

Pure stdlib, no real model: the end-to-end half runs the REAL fan-out (real child
processes, real loop, real tools) against a fake OpenAI-compatible server on localhost
that scripts each conversation by its question. Lifecycle probes, not a compile check:
  E2  a model that tries write_file must not change the tree, and the run still answers
  E3  finish with an empty report is a FAILURE, never a quiet ok
  E5  out of turns -> the last-call turn still gets the report out
  E6  a 429 from z.ai's concurrency cap is retried, not fatal
  E8  the parallel children really overlap (the point of a fan-out)
  U5  a file deleted from the tree but still in the git index is not offered
"""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
SMITH = os.path.join(_HERE, "smith_agent.py")
spec = importlib.util.spec_from_file_location("smith_agent", SMITH)
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

all_pass = True


def check(ok, what):
    global all_pass
    print(("PASS  " if ok else "FAIL  ") + what)
    all_pass = all_pass and bool(ok)


GITENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
              GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def make_repo():
    root = tempfile.mkdtemp(prefix="explore-")
    files = {
        "pkg/mod.py": "import os\ndef alpha(x):\n    return x + 1\n\ndef beta():\n    pass\n",
        "pkg/other.py": "from pkg.mod import alpha\nprint(alpha(2))\n",
        "README.md": "# demo\nalpha does things\n",
        "big.txt": "".join(f"line {i} filler filler filler filler\n" for i in range(1, 2001)),
        "build/out.py": "def alpha(): pass  # ignored build output\n",
        ".gitignore": "build/\n",
        "gone.py": "def alpha(): pass\n",
    }
    for rel, body in files.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w") as f:
            f.write(body)
    with open(os.path.join(root, "blob.bin"), "wb") as f:
        f.write(b"alpha\0\x01\x02")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, env=GITENV)
    os.remove(os.path.join(root, "gone.py"))   # still in the index, gone from the tree
    return os.path.realpath(root)


# ---------------------------------------------------------------- unit probes

root = make_repo()
sm._INDEX.clear()
idx = sm.explore_index(root)
check("pkg/mod.py" in idx and "README.md" in idx, "U1 index lists tracked files")
check(not any(f.startswith("build/") for f in idx), "U2 index honours .gitignore")
check("gone.py" not in idx, "U5 a file deleted from the tree is not offered (stale index)")

g = sm.t_x_grep(root, r"def alpha")
check("pkg/mod.py:2:" in g, "U3 grep finds path:line")
check("build/out.py" not in g and "gone.py" not in g, "U3 grep skips ignored + deleted files")
check("blob.bin" not in sm.t_x_grep(root, "alpha"), "U3 grep skips binary files")
check("pkg/other.py" not in sm.t_x_grep(root, "alpha", glob="mod.py"), "U3 grep glob filter")
check("README.md" not in sm.t_x_grep(root, "alpha", path="pkg"), "U3 grep path filter")
check("invalid regex" in sm.t_x_grep(root, "alpha("), "U3 bad regex falls back to literal")
check(sm.t_x_grep(root, "ALPHA").startswith("(no matches"), "U3 case-sensitive by default")
check("pkg/mod.py:2" in sm.t_x_grep(root, "ALPHA", ignore_case=True), "U3 ignore_case works")
check("escapes" in sm.t_x_grep(root, "x", path="../"), "U4 grep refuses a path outside root")

r = sm.t_x_read(root, "pkg/mod.py", 2, 3)
check(r.splitlines()[0].strip().startswith("2|") and len(r.splitlines()) == 2,
      "U6 read_file returns the numbered range")
big = sm.t_x_read(root, "big.txt")
check("start_line=" in big.splitlines()[-1], "U6 truncated read names the line to continue at")
check("escapes" in sm.t_x_read(root, "/etc/passwd"), "U4 read refuses an absolute path")
check("escapes" in sm.t_x_read(root, "../x"), "U4 read refuses ..")
check("no lines in range" in sm.t_x_read(root, "pkg/mod.py", 50), "U6 out-of-range read says so")
check("pkg/mod.py" in sm.t_x_list(root, "pkg") and "README" not in sm.t_x_list(root, "pkg"),
      "U7 list_files scoped to a directory")

c = sm.check_citations(root, "see pkg/mod.py:2 and mod.py:5 and pkg/mod.py:2-3, "
                             "but also nope.py:9 and pkg/mod.py:99")
check(c["valid"] == 3 and c["invalid"] == 2, f"U8 citation check counts real vs fake ({c})")

check(sm.parse_questions("a?\n# skip\n\nb?\n") == ["a?", "b?"], "U9 one question per line")
check(sm.parse_questions("multi\nline\n---\nsecond\n") == ["multi\nline", "second"],
      "U9 '---' separates multi-line questions")


class A:
    pass


def metered(backend, model, base_url="http://localhost:8080"):
    a = A()
    a.backend, a.model, a.base_url = backend, model, base_url
    return sm._is_metered(a)


check(not metered("ollama", "gpt-oss:20b"), "U10 local ollama is free")
check(metered("ollama", "glm-5.3:cloud") and metered("ollama", "gpt-oss:20b-cloud"),
      "U10 both Ollama Cloud spellings are metered")
check(not metered("openai", "glm-5.3", sm.ZAI_CODING_URL), "U10 z.ai Coding Plan is free")
check(metered("gemini", "pro") and metered("openai", "x", "https://api.example.com/v1"),
      "U10 Gemini API and unknown hosts are metered")

# ---------------------------------------------------------------- fake server

state = {"inflight": 0, "max_inflight": 0, "tools_seen": set(), "busy_429": 0}
lock = threading.Lock()


def call(name, args):
    return {"id": "c" + str(time.time_ns()), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def script(messages):
    """Scripted assistant reply, chosen by the question and how far the run has got."""
    q = next(m["content"] for m in messages if m["role"] == "user")
    turn = sum(1 for m in messages if m["role"] == "assistant")
    last = messages[-1].get("content") or ""
    if "alpha" in q:
        steps = [call("grep", {"pattern": "def alpha"}),
                 call("read_file", {"path": "pkg/mod.py", "start_line": 1, "end_line": 4}),
                 call("finish", {"report": "alpha is at pkg/mod.py:2: `def alpha(x):`, "
                                           "used in pkg/other.py:2."})]
        return {"content": "", "tool_calls": [steps[min(turn, 2)]]}
    if "empty" in q:
        return {"content": "", "tool_calls": [call("finish", {"report": "   "})]}
    if "writer" in q:
        if turn == 0:
            return {"content": "", "tool_calls": [call("write_file", {"path": "pwned.txt",
                                                                       "content": "x"})]}
        return {"content": "", "tool_calls": [call("finish", {"report": "nope.py:9 is it"})]}
    if "prose" in q:
        return {"content": "The answer, in prose rather than a finish call. " * 10,
                "tool_calls": []}
    if "loop" in q:
        if "turn budget is used up" in last:
            return {"content": "", "tool_calls": [call("finish", {"report": "partial: "
                                                                   "pkg/mod.py:5 has beta"})]}
        return {"content": "", "tool_calls": [call("grep", {"pattern": "beta"})]}
    if "busy" in q:
        return {"content": "", "tool_calls": [call("finish", {"report": "README.md:1"})]}
    return {"content": "", "tool_calls": [call("finish", {"report": "?"})]}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        q = next(m["content"] for m in body["messages"] if m["role"] == "user")
        with lock:
            for t in body.get("tools") or []:
                state["tools_seen"].add(t["function"]["name"])
            if "busy" in q and state["busy_429"] == 0:
                state["busy_429"] += 1
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'{"error":{"code":"1302","message":"Rate limit"}}')
                return
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        time.sleep(0.4)   # long enough for parallel children to overlap
        msg = script(body["messages"])
        with lock:
            state["inflight"] -= 1
        out = json.dumps({"choices": [{"message": {"role": "assistant", **msg}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"

work = tempfile.mkdtemp(prefix="fanout-")
ledger = os.path.join(work, "usage.jsonl")
qfile = os.path.join(work, "q.txt")
with open(qfile, "w") as f:
    f.write("Where is alpha defined?\n---\nempty please\n---\nwriter test\n---\n"
            "prose answer\n---\nloop forever\n---\nbusy server\n")
out = os.path.join(work, "out")
env = dict(os.environ, SMITH_LEDGER=ledger)
p = subprocess.run([sys.executable, "-B", SMITH, "--explore", root, "--fanout", qfile,
                    "--backend", "openai", "--base-url", URL, "--model", "fake",
                    "--max-turns", "2", "-j", "4", "--out-dir", out],
                   capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                   timeout=180)
try:
    summ = json.loads(p.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    summ = {}
    print(p.stdout, p.stderr)
failed = {f["q"]: f["stop"] for f in summ.get("failed", [])}

check(summ.get("fanout") == 6 and summ.get("ok") == 5, f"E1 6 questions, 5 ok ({summ})")
check(p.returncode == 1, "E1 a fan-out with a failure exits 1, not 0")
check(os.path.getsize(os.path.join(out, "q01-where-is-alpha-defined.md")) > 0,
      "E1 the real-tool conversation produced its report file")
check("write_file" not in state["tools_seen"] and "run_command" not in state["tools_seen"]
      and "grep" in state["tools_seen"], "E2 explore sends no write/shell tool to the model")
check(not os.path.exists(os.path.join(root, "pwned.txt")),
      "E2 a write_file attempt changed nothing on disk")
check(failed.get("q02-empty-please") == "finish_empty_report",
      "E3 finish with an empty report is a failure")
combined = open(summ.get("combined") or os.devnull).read()
check("nope.py:9" in combined and "0 valid / 1 invalid" in combined,
      "E4 an invented citation is flagged in fanout.md")
check("q04-prose-answer" not in failed, "E4 a substantive prose answer counts as the report")
check("q05-loop-forever" not in failed and "partial: pkg/mod.py:5" in combined,
      "E5 out of turns -> last-call turn still returns the report")
check("q06-busy-server" not in failed and state["busy_429"] == 1,
      "E6 a 429 is retried, not fatal")
check(state["max_inflight"] >= 2, f"E8 children overlapped (max in flight "
                                  f"{state['max_inflight']})")

rows = [json.loads(ln) for ln in open(ledger)] if os.path.exists(ledger) else []
check(len(rows) == 6 and all(r.get("tag") == "subagent-fanout" for r in rows),
      "E7 one ledger row per agent, tagged subagent-fanout")
check(all(r.get("fanout") == "out" for r in rows), "E7 ledger rows carry the fan-out id")
keys = [(r.get("ts"), r.get("script"), r.get("model")) for r in rows]
check(len(set(keys)) == len(keys), "E10 every agent's ledger key (ts, script, model) is unique, "
                                   "so a verdict grades exactly one run")
good = [r for r in rows if r.get("finished")]
check(good and all(r.get("output_file") and os.path.isfile(r["output_file"]) for r in good),
      "E7 every finished row points at a report that exists (verdictable)")

# Cost gate: a metered route never starts a fan-out without --allow-metered.
p = subprocess.run([sys.executable, "-B", SMITH, "--explore", root, "--fanout", qfile,
                    "--backend", "gemini", "--model", "pro"],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
                   env=dict(env, GEMINI_API_KEY=""))
check(p.returncode == 2 and "allow-metered" in p.stderr,
      "E9 a metered fan-out is refused before any call")
p = subprocess.run([sys.executable, "-B", SMITH, "--workdir", work, "--fanout", qfile,
                    "--model", "x"], capture_output=True, text=True, timeout=30)
check(p.returncode == 2 and "only apply to --explore" in p.stderr,
      "E9 --fanout without --explore is refused")

srv.shutdown()
print("\nALL PASS" if all_pass else "\nSOME FAILED")
sys.exit(0 if all_pass else 1)
