#!/usr/bin/env python3
"""Probes for how grades count (grades.py) and where they are kept (verdict_db.py). No model calls.

Found 2026-10-25 while writing Lab 06: usage_report.py and gap_report.py kept only the LAST
grade on each run, so every earlier grade on a run that carried several was dropped without
a word (87 of 335 incidents on that day's ledger), and the two reports did not even agree:
gap_report counted rows, usage_report counted incidents. verdicts.db keyed on ts, so two
grades written in the same second overwrote each other (it held 328 of 411).
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import grades  # noqa: E402

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def run(ts, model="m1", tag="research"):
    return {"ts": ts, "script": "gemini", "model": model, "tag": tag, "purpose": "probe", "status": "ok"}


def grade(ts, ref, verdict, note="", tag="research", **extra):
    return {"ts": ts, "script": "verdict", "verdict": verdict, "note": note, "tag": tag,
            "ref_ts": ref["ts"], "ref_script": ref["script"], "ref_model": ref["model"], **extra}


A, B, C = run("2026-10-01T10:00:00"), run("2026-10-01T11:00:00"), run("2026-10-01T12:00:00")
LEDGER = [
    A, B, C,
    grade("2026-10-01T10:05:00", A, "good", "first answer fine"),
    grade("2026-10-01T10:06:00", A, "bad", "second answer invented a source"),   # same run, a second judgment
    grade("2026-10-01T11:05:00", B, "bad", "invented a source"),
    grade("2026-10-01T12:05:00", C, "bad", "Invented   a source"),             # same incident as B's (note normalised)
    grade("2026-10-01T12:06:00", C, "good", "dup", duplicate_of="2026-10-01T12:05:30"),
    grade("2026-10-01T12:07:00", C, "stale", "gone"),
]

pairs = grades.judged([A, B, C], [r for r in LEDGER if r["script"] == "verdict"])
check(len(pairs) == 4, f"judged: 4 good/bad grades, duplicate and stale left out (got {len(pairs)})")
check([v["verdict"] for r, v in pairs if r is A] == ["good", "bad"], "both grades on one run count, in grade order")
routes, collapsed = grades.routes(pairs)
chain = routes.get(("research", "m1"), [])
check([v for _, v in chain] == ["good", "bad", "bad"], f"one incident per route: {chain}")
check(collapsed.get(("research", "m1")) == 1, "the repeated incident is reported as collapsed")
check(grades.latest(LEDGER[3:])[grades.run_key(C)]["verdict"] == "stale", "latest(): last grade per run, stale included")

with tempfile.TemporaryDirectory() as d:
    ledger = os.path.join(d, "usage.jsonl")
    with open(ledger, "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in LEDGER))
        f.write("not json\n")
    env = dict(os.environ, SMITH_LEDGER=ledger, SMITH_VERDICT_DB=os.path.join(d, "verdicts.db"))

    out = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "usage_report.py")], env=env,
                         capture_output=True, text=True).stdout
    m = re.search(r"research @ m1: (\d+) good, (\d+) bad", out)
    check(m and (int(m[1]), int(m[2])) == (1, 2), f"usage_report routing weights 1 good 2 bad: {m and m[0]}")
    check("verified: 1 good, 3 bad grades" in out, "usage_report verified line counts every grade")

    audit = os.path.join(d, "audit.json")
    json.dump({"tool_calls": [["WebSearch", 3]], "tool_result_chars": []}, open(audit, "w"))
    out = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "gap_report.py"), "--audit", audit],
                         env=env, capture_output=True, text=True).stdout
    check("m1 1g/2b" in out, "gap_report trust agrees with usage_report: m1 1g/2b")

    # The mirror: same-second grades both kept, an old-schema mirror rebuilt, inserts idempotent.
    db = env["SMITH_VERDICT_DB"]
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE verdicts (ts TEXT PRIMARY KEY, verdict TEXT NOT NULL, note TEXT, tag TEXT, "
                "ref_ts TEXT, ref_model TEXT, ref_script TEXT)")
    old.commit()
    old.close()
    probe = (f"import sys; sys.path.insert(0, {_HERE!r}); import verdict_db, json;"
             f"rec = json.loads({json.dumps(json.dumps(LEDGER[4]))});"
             f"verdict_db.insert(rec); verdict_db.insert(rec);"
             f"same = dict(rec, ref_ts='2026-10-01T11:00:00', note='other run, same second');"
             f"verdict_db.insert(same);"
             f"c = verdict_db.connect(); print(c.execute('select count(*) from verdicts').fetchone()[0])")
    n = subprocess.run([sys.executable, "-B", "-c", probe], env=env, capture_output=True, text=True)
    check(n.stdout.strip() == "7", f"mirror: old schema rebuilt (6), re-insert skipped, same-second grade kept: {n.stdout.strip() or n.stderr[-200:]}")

# No report may count from a map keyed by run again.
for name in ("usage_report.py", "gap_report.py"):
    src = open(os.path.join(_HERE, name), encoding="utf-8").read()
    check('verdicts[(r.get("ref_ts")' not in src and "verdicts[key] = r" not in src and "import grades" in src,
          f"{name} counts through grades.py, not a last-grade-per-run map")

sys.exit(0 if all_pass else 1)
