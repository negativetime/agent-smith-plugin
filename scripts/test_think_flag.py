#!/usr/bin/env python3
"""Unit test for --think (Ollama's `think` field) in gemini.py and smith_agent.py.

Pure stdlib, no model calls: urlopen is swapped for a fake that records the request body.
Added 2026-09-11 with the flag. deepseek-v4.1-flash loops forever in its thinking channel at
temperature 0 on a dense spec and passes the same spec in 1.1s with thinking off, and until
then neither script could send the switch on the ollama backend.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gemini = _load("gemini")
smith = _load("smith_agent")

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(payload):
    """Swap urlopen for a fake answering `payload`; returns the list of request bodies."""
    sent = []

    def fake(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        return _Resp(payload)

    urllib.request.urlopen = fake
    return sent


_real_urlopen = urllib.request.urlopen
MODEL = "deepseek-v4.1-flash:cloud"
ANSWER = {"model": MODEL, "message": {"role": "assistant", "content": "PONG"},
          "prompt_eval_count": 5, "eval_count": 2}

try:
    # 1. flag value -> wire value
    for choice, want in ((None, None), ("on", True), ("off", False), ("low", "low"),
                         ("max", "max")):
        check(gemini._think_value(choice) == want and type(gemini._think_value(choice)) is type(want),
              f"_think_value({choice!r}) -> {want!r}")

    # 2-4. call_ollama puts it in the body, and leaves the default request untouched
    for think, present in ((None, False), (False, True), ("low", True)):
        sent = _capture(ANSWER)
        with contextlib.redirect_stderr(io.StringIO()):
            out = gemini.call_ollama("hi", None, 0.0, MODEL, None, think=think)
        body = sent[-1]
        ok = ("think" in body) == present and (not present or body["think"] == think)
        check(ok and out == "PONG", f"call_ollama think={think!r} -> body has think={body.get('think', '<absent>')!r}")

    # 5-6. a model that ignores think=false is flagged; an honest one is not
    for thinking, warned in (("hidden reasoning", True), ("", False)):
        _capture({**ANSWER, "message": {**ANSWER["message"], "thinking": thinking}})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gemini.call_ollama("hi", None, 0.0, MODEL, None, think=False)
        check(("ignored think=false" in err.getvalue()) == warned,
              f"think=false with {len(thinking)} thinking chars -> warning {'fires' if warned else 'silent'}")

    # 7. smith_agent's tool loop sends it too
    for think, present in ((None, False), (False, True)):
        smith.THINK = think
        sent = _capture(ANSWER)
        smith.chat(MODEL, [{"role": "user", "content": "hi"}], 8192, tools=False)
        check(("think" in sent[-1]) == present and sent[-1].get("think", None) == think,
              f"smith_agent.chat THINK={think!r} -> body has think={sent[-1].get('think', '<absent>')!r}")
    smith.THINK = None
finally:
    urllib.request.urlopen = _real_urlopen

# 8. the flag is refused where it cannot apply, before any network call
env = dict(os.environ, SMITH_LEDGER=os.devnull)
r = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "gemini.py"), "--backend", "gemini",
                    "--think", "off", "--tag", "smoke", "x"],
                   capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
check(r.returncode == 2 and "--think" in r.stderr, f"gemini.py --backend gemini --think off -> exit {r.returncode}")
r = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "smith_agent.py"), "--backend", "gemini",
                    "--think", "off", "--model", "m", "--workdir", ".", "--prompt-file", os.devnull],
                   capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
check(r.returncode == 2 and "--think" in r.stderr, f"smith_agent.py --backend gemini --think off -> exit {r.returncode}")

# 9. every ollama call site in gemini.py forwards the flag (the witness deliberately does not)
src = open(os.path.join(_HERE, "gemini.py"), encoding="utf-8").read()
check(src.count("think=_think_value(args.think)") == 2,
      "both call_ollama call sites (one-shot + batch/consensus) forward --think")

sys.exit(0 if all_pass else 1)
