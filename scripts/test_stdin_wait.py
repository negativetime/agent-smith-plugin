#!/usr/bin/env python3
"""Probes for gemini.py's stdin wait notice. No model call, no request that can succeed: the
backend points at a closed local port, and every case writes to a temp SMITH_LEDGER.

Why it exists (2026-11-07): gemini.py reads stdin whenever stdin is not a terminal. A background
shell's stdin is a socket that never closes, so a run with a prompt argument and nothing piped in
blocked in read() for 9 minutes with an empty stderr, which looked exactly like a slow API (two
doc-format runs were stopped before the cause was found with `sample`). A timeout cannot fix it,
since a slow producer may send its first byte minutes later, so it is not one: the run says it is
waiting, and how to stop waiting.

    python3 -B scripts/test_stdin_wait.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# A stand-in OpenAI-compatible endpoint on loopback: it records every request body and answers
# with a canned completion, so the late-input case can show what reached the backend.
BODIES = []


class _Stub(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        BODIES.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace"))
        out = json.dumps({"model": "probe", "choices": [{"message": {"content": "ok"},
                                                         "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="stdinwait-")
env = dict(os.environ, SMITH_LEDGER=os.path.join(tmp, "usage.jsonl"), SMITH_NO_ARCHIVE="1",
           SMITH_STDIN_WAIT_NOTICE="1", OPENAI_API_KEY="x")
ARGV = [sys.executable, "-B", os.path.join(_HERE, "gemini.py"), "--backend", "openai",
        "--base-url", f"http://127.0.0.1:{srv.server_port}/v1", "--model", "probe", "--tag", "classify",
        "say hi"]
NOTICE = "waiting for piped input"

# stdin open and silent (the background-shell case): the notice appears, the run is still waiting
p = subprocess.Popen(ARGV, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(3)
alive = p.poll() is None
p.kill()
_, err = p.communicate()
err = err.decode("utf-8", "replace")
check(alive, "stdin open and silent: the run is still waiting after 3 s (no timeout drops input)")
check(NOTICE in err and "< /dev/null" in err, "...and stderr says it is waiting, and how to stop waiting")
check(err.count(NOTICE) == 1, "...once, not once per second")

# stdin at EOF at once: no notice
p = subprocess.run(ARGV, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
check(NOTICE not in p.stderr.decode("utf-8", "replace"), "stdin at EOF (< /dev/null): no notice")

# data that arrives late is still read: the wait never cuts it off
n_before = len(BODIES)          # before the run starts, so a fast request cannot slip past it
p = subprocess.Popen(ARGV, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(2)
p.stdin.write(b"late input\n")
p.stdin.close()
out, err = p.communicate(timeout=60)
err = err.decode("utf-8", "replace")
sent = BODIES[n_before:]
check(NOTICE in err and p.returncode == 0 and out.decode().strip() == "ok",
      "input that arrives after the notice is still read, and the run finishes (exit 0, the answer)")
check(len(sent) == 1 and "late input" in sent[0] and "say hi" in sent[0],
      "...and the late input reached the backend, below the prompt argument")

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)

print("\nALL PASS" if all_pass else "\nSOME FAILED")
sys.exit(0 if all_pass else 1)
