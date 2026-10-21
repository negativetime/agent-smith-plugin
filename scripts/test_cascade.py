#!/usr/bin/env python3
"""Probes for the escalation ladder (cascade.py + gemini.py --cascade). Pure stdlib, no model
calls: every rung call is replaced by a fake, so this proves the WIRING (gates fire, only the
winner reaches stdout, ledger rows tie together, the SoundCheck guard drops cloud rungs).

    python3 -B scripts/test_cascade.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import cascade  # noqa: E402

_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# ---- gates ---------------------------------------------------------------------------
c = cascade.check_output
check(c("") == "empty" and c("  \n") == "empty" and c(None) == "empty", "gate: empty / whitespace / None")
check(c("partial", finish_reason="length") == "truncated", "gate: finish_reason=length is truncated")
check(c("ok", finish_reason="stop") is None, "gate: finish_reason=stop passes")
check(c("not json", want_json=True) == "bad-json", "gate: bad json")
check(c('```json\n{"a": 1}\n```', want_json=True) is None, "gate: fenced json passes")
check((c("def f(:\n  pass", want_python=True) or "").startswith("py-syntax"), "gate: python syntax error")
check(c("```python\ndef f():\n    return 1\n```", want_python=True) is None, "gate: fenced python passes")
check(c("x", extra=(lambda t: "nope",)) == "nope", "gate: extra callable can veto")
check(gemini._agentic_gate("I have created the file as requested.") == "agentic-nonanswer",
      "gate: agentic non-answer is caught")

# ---- ladder --------------------------------------------------------------------------
check(cascade.build_ladder("classify") == ["T1", "T2"], "ladder: classify starts at T1 (T0 failed gym struct_classify)")
check(cascade.build_ladder("classify", explicit="T0,T1,T2") == ["T0", "T1", "T2"], "ladder: T0 still reachable explicitly")
check(cascade.build_ladder("never-heard-of-it") == ["T2", "T3"], "ladder: unknown tag starts at T2")
check(cascade.build_ladder("code-draft", start="T3") == ["T3"], "ladder: --start drops lower rungs")
check(cascade.build_ladder("x", explicit="t1,t2") == ["T1", "T2"], "ladder: explicit, case-folded")
check(cascade.build_ladder("doc-format", explicit="T1,T2") == ["T2"], "ladder: blocked (T1, doc-format) removed")
for bad in ("T9", "T1,T1", ""):
    try:
        cascade.build_ladder("x", explicit=bad)
        check(False, f"ladder: {bad!r} should raise")
    except ValueError:
        check(True, f"ladder: {bad!r} raises")
try:
    cascade.build_ladder("code-draft", start="T1")
    check(False, "ladder: --start off the ladder should raise")
except ValueError:
    check(True, "ladder: --start off the ladder raises")

# ---- load gate / floors --------------------------------------------------------------
check(cascade.local_unhealthy(2.0, 12, True) is None, "load: calm Mac is fine")
check("load" in (cascade.local_unhealthy(23.0, 12, True) or ""), "load: 23 on 12 cores is skipped")
check("ollama" in (cascade.local_unhealthy(1.0, 12, False) or ""), "load: ollama down is skipped")
check(cascade.local_unhealthy(18.0, 12, True) is None, "load: exactly 1.5x is still allowed")
check(cascade.floor_max_tokens("T2", None) == 32000 and cascade.floor_max_tokens("T2", 100) == 32000,
      "floor: raises a low/absent max_tokens")
check(cascade.floor_max_tokens("T2", 50000) == 50000, "floor: never lowers an explicit value")
check(cascade.floor_max_tokens("T0", None) is None, "floor: T0 has none")

# ---- report --------------------------------------------------------------------------
rows = []
for n in range(6):
    cid = f"c{n}"
    rows.append({"cascade_id": cid, "script": "cascade-attempt", "status": "gate_fail", "tag": "classify",
                 "rung": "T0", "rung_index": 0, "gate_reason": "empty"})
    rows.append({"cascade_id": cid, "script": "gemini", "tag": "classify", "rung": "T1"})
rows += [{"cascade_id": "z", "script": "cascade-attempt", "status": "exhausted", "tag": "classify",
          "rung": "-", "rung_index": 3, "gate_reason": "exhausted"}]
st = cascade.summarize(rows)["classify"]
check(st["runs"] == 7 and st["escalated"] == 6 and st["exhausted"] == 1,
      f"report: counts escalated/exhausted ({st['runs']}/{st['escalated']}/{st['exhausted']})")
check(st["first_rung_fail"] == {"T0": 6}, "report: the exhausted marker is not a failed rung")
check(any("T0" in a for a in cascade.advice({"classify": st})), "report: advises starting above T0")
check(cascade.summarize([{"cascade_id": "s", "script": "cascade-attempt", "status": "gate_fail",
                          "tag": "smoke", "rung": "T1", "rung_index": 0}]) == {},
      "report: smoke-tagged cascades are ignored")

# ---- end to end through main() -------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="cascade-test-")
os.environ["SMITH_LEDGER"] = os.path.join(tmp, "usage.jsonl")
os.environ["SMITH_NO_ARCHIVE"] = "1"
os.environ["ZAI_API_KEY"] = "test"
os.environ["SMITH_WITNESS_RATE"] = "0"
gemini._witness = lambda *a, **k: None          # the witness would make a real model call


def run(argv, replies, probe=(1.0, 12, True), cwd=None):
    """Run main() with fake rungs. replies: {rung name: text | (text, finish) | 'EXIT'}."""
    calls = []

    def fake(rung, name, prompt, sys_eff, args, max_tokens):
        calls.append(name)
        r = replies.get(name, "")
        if r == "EXIT":
            sys.exit(1)
        if isinstance(r, tuple):
            gemini._LAST_FINISH = r[1]
            return r[0]
        gemini._LAST_FINISH = "stop"
        return r

    gemini._call_rung, gemini._probe_local = fake, lambda: probe
    out, err = io.StringIO(), io.StringIO()
    old_argv, old_cwd, code = sys.argv, os.getcwd(), 0
    sys.argv = ["gemini.py"] + argv
    if cwd:
        os.chdir(cwd)
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                gemini.main()
            except SystemExit as e:
                code = e.code or 0
    finally:
        sys.argv = old_argv
        sys.stdin = sys.__stdin__
        os.chdir(old_cwd)
    return code, out.getvalue(), err.getvalue(), calls


def ledger():
    with open(os.environ["SMITH_LEDGER"]) as f:
        return [json.loads(x) for x in f if x.strip()]


code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"],
                            {"T0": "", "T1": "", "T2": "answer from glm"})
check(code == 0 and out.strip() == "answer from glm", "e2e: empty T0 and T1 escalate to T2; ONLY the winner prints")
check(calls == ["T0", "T1", "T2"], f"e2e: rungs tried in order {calls}")
rows = ledger()
att = [r for r in rows if r["script"] == "cascade-attempt"]
win = [r for r in rows if r["script"] == "gemini"]
check(len(att) == 2 and len(win) == 1 and len({r["cascade_id"] for r in rows}) == 1,
      "e2e: 2 attempt rows + 1 winner share one cascade_id")
check(win[0]["rung"] == "T2" and win[0]["cascade"] == ["T0:empty", "T1:empty", "T2:ok"],
      f"e2e: winner row records the path {win[0].get('cascade')}")
check(gemini._unreviewed_count(os.environ["SMITH_LEDGER"]) == 1, "e2e: attempt rows stay out of the review queue")

code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"],
                            {"T0": ("partial", "length"), "T1": "full answer"})
check(out.strip() == "full answer" and calls == ["T0", "T1"], "e2e: a truncated answer (finish_reason=length) escalates")

code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"],
                            {"T0": "EXIT", "T1": "recovered"})
check(out.strip() == "recovered" and "call-failed" in err, "e2e: a rung that exits is an escalation, not a crash")

code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"], {"T2": "cloud only"}, probe=(23.0, 12, True))
check(calls == ["T2"] and out.strip() == "cloud only", f"e2e: overloaded Mac skips BOTH local rungs {calls}")

code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"], {"T2": "cloud only"}, probe=(1.0, 12, False))
check(calls == ["T2"], "e2e: Ollama down skips the local rungs")

os.environ.pop("ZAI_API_KEY")
code, out, err, calls = run(["--cascade", "--tag", "code-draft", "go"], {"T3": "gemini text"})
check(calls == ["T3"] and "ZAI_API_KEY is not set" in err, "e2e: no ZAI key skips T2 and says why")
os.environ["ZAI_API_KEY"] = "test"

code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"], {})
check(code == 1 and out == "" and calls == ["T0", "T1", "T2"], "e2e: every rung failing exits 1 with empty stdout")
check(ledger()[-1]["status"] == "exhausted", "e2e: exhaustion is ledgered")

code, out, err, calls = run(["--cascade", "--tag", "code-draft", "--gate", "python", "go"],
                            {"T2": "def f(:\n  pass", "T3": "def f():\n    return 1"})
check(out.strip() == "def f():\n    return 1" and calls == ["T2", "T3"], "e2e: --gate python escalates on a syntax error")

# SoundCheck: cloud rungs are DROPPED, never used, never silently rerouted.
sc = os.path.join(tmp, "soundcheck-stuff")
os.makedirs(sc)
code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"],
                            {"T0": "", "T1": "", "T2": "MUST NOT BE REACHED"}, cwd=sc)
check(code == 1 and "T2" not in calls, f"soundcheck: classify ladder stops at local rungs {calls}")
code, out, err, calls = run(["--cascade", "--tag", "code-draft", "go"], {"T2": "x", "T3": "x"}, cwd=sc)
check(code == 2 and calls == [], "soundcheck: an all-cloud ladder fails closed with no call made")

# Flag conflicts are refused, not ignored.
for flags in (["--model", "pro"], ["--backend", "ollama"], ["--search"], ["--think", "low"],
              ["--schema", "s.json"], ["--file", "x.png"]):
    code, out, err, calls = run(["--cascade", "--ladder", "T0,T1,T2", "--tag", "classify", "go"] + flags, {})
    check(code == 2 and calls == [], f"flags: --cascade with {flags[0]} is refused")
code, out, err, calls = run(["--ladder", "T1", "--tag", "classify", "go"], {})
check(code == 2, "flags: --ladder without --cascade is refused")

# Without --cascade nothing changed: the tag tables still resolve the route.
src = open(os.path.join(_HERE, "gemini.py"), encoding="utf-8").read()
check("if args.cascade:\n        pass" in src and src.count("        run_cascade(args, prompt, t0)\n        return") == 1,
      "wiring: cascade branch is opt-in and called once")

sys.exit(0 if all_pass else 1)
