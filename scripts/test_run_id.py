#!/usr/bin/env python3
"""Probes for run identity: every ledger row gets a run_id, and verdict.py grades one run, never
whichever of several runs that finished in the same second. Pure stdlib, no model calls.

Found 2026-10-28 by the Lab 06 blind regrade: a run was named by (ts, script, model), and ts has
only seconds resolution, so two parallel runs of one model that finished in the same second were
one run to verdict.py --ts and to every count. A grade on one could not be told from a grade on
the other, and one sampled run had to be set aside.

    python3 -B scripts/test_run_id.py
"""
import datetime
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import grades  # noqa: E402

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")]


def write(path, recs):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)


def verdict(env, *argv):
    p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "verdict.py"), *argv],
                       env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


# ---- every writer stamps its own run_id ------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "usage.jsonl")
    os.environ["SMITH_LEDGER"], os.environ["SMITH_NO_ARCHIVE"] = ledger, "1"
    for name in ("gemini", "smith_agent", "transcribe", "embed"):
        mod = load(name)
        mod._ledger({"script": name, "model": "m", "tag": "probe"})
        mod._ledger({"script": name, "model": "m", "tag": "probe"})
    got = rows(ledger)
    ids = [r.get("run_id") for r in got]
    check(len(got) == 8 and all(ids), "gemini, smith_agent, transcribe and embed each stamp a run_id")
    check(len(set(ids)) == 8, "two rows from one writer in the same second get different run_ids")
    del os.environ["SMITH_LEDGER"], os.environ["SMITH_NO_ARCHIVE"]

# ---- verdict.py: two runs of one model in one second -----------------------------------------
now = datetime.datetime.now().replace(microsecond=0)
T, T_EARLY = now.isoformat(), (now - datetime.timedelta(minutes=5)).isoformat()
A = {"ts": T, "run_id": "aaaaaaaaaaaa", "script": "gemini", "model": "glm-5.3", "tag": "classify",
     "purpose": "first of two parallel runs", "output_file": "outputs/x.txt"}
B = {"ts": T, "run_id": "bbbbbbbbbbbb", "script": "gemini", "model": "glm-5.3", "tag": "classify",
     "purpose": "second of two parallel runs", "output_file": "outputs/x-2.txt"}
C = {"ts": T_EARLY, "run_id": "cccccccccccc", "script": "gemini", "model": "gemini-pro-latest",
     "tag": "research", "purpose": "an earlier run"}

with tempfile.TemporaryDirectory() as tmp:
    ledger, db = os.path.join(tmp, "usage.jsonl"), os.path.join(tmp, "verdicts.db")
    write(ledger, [C, A, B])
    # A mirror on the schema before ref_run_id existed: it must be rebuilt, not written into blind.
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, verdict TEXT NOT NULL, "
                "note TEXT, tag TEXT, ref_ts TEXT, ref_model TEXT, ref_script TEXT)")
    old.commit(); old.close()
    env = dict(os.environ, SMITH_LEDGER=ledger, SMITH_VERDICT_DB=db)
    before = open(ledger).read()

    rc, out = verdict(env, "good", "--ts", T, "--tag", "classify")
    check(rc == 1 and "aaaaaaaaaaaa" in out and "bbbbbbbbbbbb" in out and "REFUSED" in out,
          "--ts that fits two runs is refused, and both run_ids are listed")
    rc2, _ = verdict(env, "good", "--model", "glm-5.3")
    rc3, _ = verdict(env, "good")
    check(rc2 == 1 and rc3 == 1, "'most recent' that fits two runs is refused too (by model, and bare)")
    rc4, out4 = verdict(env, "good", "--run-id", "nonexistent0")
    check(rc4 == 1 and open(ledger).read() == before, "an unknown --run-id grades nothing")
    check(open(ledger).read() == before, "refusals write nothing to the ledger")

    rc, out = verdict(env, "bad", "read the second one", "--run-id", "bbbbbbbbbbbb", "--tag", "classify")
    g = rows(ledger)[-1]
    check(rc == 0 and g.get("ref_run_id") == "bbbbbbbbbbbb" and g.get("ref_ts") == T,
          "--run-id grades that run, and the grade keeps ref_ts too")

    runs, grade_rows, _ = grades.read(ledger)
    pairs = grades.judged(runs, grade_rows)
    check([r["run_id"] for r, _ in pairs] == ["bbbbbbbbbbbb"], "judged() pairs the grade with B alone")
    seen = grades.latest(grade_rows)
    check(grades.grade_of(seen, B) is not None and grades.grade_of(seen, A) is None,
          "B shows as graded and its same-second sibling A does not")
    check(load("gemini")._unreviewed_count(ledger) == 2, "the review nag still counts A (and C) as waiting")
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(verdicts)")]
    got = sqlite3.connect(db).execute("SELECT ref_run_id FROM verdicts").fetchall()
    check("ref_run_id" in cols and got == [("bbbbbbbbbbbb",)], "verdicts.db was rebuilt with ref_run_id and holds it")

    rc, _ = verdict(env, "good", "--run-id", "aaaaaaaaaaaa", "--tag", "classify")
    runs, grade_rows, _ = grades.read(ledger)
    check(rc == 0 and sorted(r["run_id"] for r, _ in grades.judged(runs, grade_rows)) == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
          "each run keeps its own grade once both are graded")

# ---- rows older than run_id keep working as before -------------------------------------------
L1 = {"ts": T, "script": "gemini", "model": "glm-5.3", "tag": "translate", "purpose": "legacy one"}
L2 = {"ts": T, "script": "gemini", "model": "glm-5.3", "tag": "translate", "purpose": "legacy two"}
L3 = {"ts": T, "script": "gemini", "model": "qwen3:8b", "tag": "translate", "purpose": "legacy other model"}
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "usage.jsonl")
    write(ledger, [L1, L2, L3])
    env = dict(os.environ, SMITH_LEDGER=ledger, SMITH_VERDICT_DB=os.path.join(tmp, "v.db"))
    rc, out = verdict(env, "good", "--ts", T)
    check(rc == 1 and "--model qwen3:8b" in out and "--model glm-5.3" in out,
          "old rows of different models in one second: refused, and --model is offered")
    rc, out = verdict(env, "good", "--ts", T, "--model", "glm-5.3", "--tag", "translate")
    g = rows(ledger)[-1]
    check(rc == 0 and "predate run ids" in out and "ref_run_id" not in g,
          "old rows of one model in one second: graded by (ts, script, model), with a warning")
    runs, grade_rows, _ = grades.read(ledger)
    check(len(grades.judged(runs, grade_rows)) == 1, "an old-style grade still counts once")

sys.exit(0 if all_pass else 1)
