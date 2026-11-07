#!/usr/bin/env python3
"""Probes for verdict.py --evidence and `verdict.py audit`. Pure stdlib, no model calls, nothing
touches the real ledger: every case runs against a temp SMITH_LEDGER.

Why this exists (2026-11-07): a grade was a tag and a free-text note, so "good" could not be told
from "good, and here is the output that shows it". The 412 good grades on that day's ledger were
doc-format 132, research 64, translate 55, code-draft 9: the proof is a resolved-URL list or a
length check at least as often as a test log. A proof that is only recorded is the house bug
class (derived state nobody re-checks), so `audit` re-opens every one.

Each refusal case asserts BOTH the exit code and that nothing was written, because a refusal
that still appends a ledger row, or leaves a proof behind, is a silent half-success.

    python3 -B scripts/test_verdict_evidence.py
"""
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def run(env, *argv, stdin=None):
    p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "verdict.py"), *argv],
                       env=env, capture_output=True, text=False,
                       input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def write_ledger(path, recs):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in recs)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def listing(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


now = datetime.datetime.now().replace(microsecond=0)


def ts(minutes):
    return (now - datetime.timedelta(minutes=minutes)).isoformat()


def run_row(rid, t, tag="code-draft"):
    return {"ts": t, "run_id": rid, "script": "gemini", "model": "glm-5.3", "tag": tag,
            "purpose": "probe", "output_file": f"outputs/{rid}.txt"}


R = {n: n * 12 for n in ("aaaa", "bbbb", "cccc", "dddd")}      # run ids, 12 chars each
SAME = ts(1)                                                    # two runs finishing in one second
RUNS = [run_row(R["aaaa"], ts(10)), run_row(R["bbbb"], ts(9)), run_row(R["cccc"], ts(8)),
        run_row(R["dddd"], ts(7)), run_row("eeeeeeeeeeee", SAME), run_row("ffffffffffff", SAME)]


def env_for(tmp):
    e = dict(os.environ)
    e["SMITH_LEDGER"] = os.path.join(tmp, "usage.jsonl")
    e["SMITH_VERDICT_DB"] = os.path.join(tmp, "verdicts.db")
    return e


with tempfile.TemporaryDirectory() as tmp:
    env = env_for(tmp)
    ledger, evdir = env["SMITH_LEDGER"], os.path.join(tmp, "evidence")
    write_ledger(ledger, RUNS)

    # ---- attach a file --------------------------------------------------------------------
    proof = os.path.join(tmp, "pytest.log")
    proof_bytes = b"12 passed in 0.41s\n"
    open(proof, "wb").write(proof_bytes)
    rc, out = run(env, "good", "--run-id", R["aaaa"], "--tag", "code-draft", "--evidence", proof)
    g = rows(ledger)[-1]
    check(rc == 0 and g.get("verdict") == "good", "good --evidence FILE records the grade")
    check(str(g.get("evidence_file", "")).startswith("evidence/") and g["evidence_file"].endswith(".log"),
          "the grade names a file under evidence/, keeping the .log suffix")
    check(g.get("evidence_sha256") == sha(proof_bytes) and g.get("evidence_bytes") == len(proof_bytes),
          "the grade records the proof's sha256 and size")
    stored = os.path.join(tmp, g.get("evidence_file", "missing"))
    check(os.path.isfile(stored) and open(stored, "rb").read() == proof_bytes,
          "the proof is copied byte for byte beside the ledger")
    check(g["evidence_file"] in out, "the confirmation line names the evidence file")

    # ---- the sqlite mirror carries it too -------------------------------------------------
    db = sqlite3.connect(env["SMITH_VERDICT_DB"])
    got = db.execute("SELECT evidence_file, evidence_sha256, evidence_bytes FROM verdicts "
                     "WHERE ref_run_id = ?", (R["aaaa"],)).fetchall()
    db.close()
    check(got == [(g["evidence_file"], sha(proof_bytes), len(proof_bytes))],
          "verdicts.db holds the same evidence fields as the ledger")

    # ---- stdin ----------------------------------------------------------------------------
    rc, out = run(env, "good", "--run-id", R["bbbb"], "--tag", "research", "--evidence", "-",
                  stdin=b"3 of 3 urls resolved (200)\n")
    g2 = rows(ledger)[-1]
    check(rc == 0 and g2.get("evidence_file", "").endswith(".txt") and g2.get("evidence_bytes") == 27,
          "--evidence - reads stdin and stores it as .txt")

    # ---- same proof on a second grade is one file ----------------------------------------
    rc, out = run(env, "bad", "tests did not run", "--run-id", R["cccc"], "--tag", "code-draft",
                  "--evidence", proof)
    g3 = rows(ledger)[-1]
    check(rc == 0 and g3["evidence_file"] == g["evidence_file"] and len(listing(evdir)) == 2,
          "the same proof attached twice is stored once (named for its content)")

    # ---- a grade with no evidence is unchanged --------------------------------------------
    rc, out = run(env, "good", "--run-id", R["dddd"], "--tag", "code-draft")
    g4 = rows(ledger)[-1]
    check(rc == 0 and not any(k.startswith("evidence") for k in g4),
          "a grade without --evidence carries no evidence fields")

    # ---- refusals: exit code AND nothing written ------------------------------------------
    def refused(what, want_rc, *argv, stdin=None):
        before_rows, before_files = len(rows(ledger)), listing(evdir)
        rc, out = run(env, *argv, stdin=stdin)
        ok = (rc == want_rc and len(rows(ledger)) == before_rows and listing(evdir) == before_files)
        check(ok, f"refused with exit {want_rc} and nothing written: {what}" + ("" if ok else f"  [rc={rc}] {out.strip()[:120]}"))

    empty, blank, big = (os.path.join(tmp, n) for n in ("empty.log", "blank.log", "big.log"))
    open(empty, "wb").close()
    open(blank, "wb").write(b"  \n\t\n")
    open(big, "wb").write(b"x" * 2_000_001)
    base = ("good", "--run-id", "eeeeeeeeeeee", "--tag", "code-draft")
    refused("missing path", 2, *base, "--evidence", os.path.join(tmp, "nope.log"))
    refused("a directory", 2, *base, "--evidence", tmp)
    refused("an empty file", 2, *base, "--evidence", empty)
    refused("a whitespace-only file", 2, *base, "--evidence", blank)
    refused("a file over the size cap", 2, *base, "--evidence", big)
    refused("empty stdin", 2, *base, "--evidence", "-", stdin=b"")
    refused("--evidence with an empty string (not silently ignored)", 2, *base, "--evidence", "")
    # a proof the store has never seen: files are named for their content, so reusing one that is
    # already stored would let a refusal that wrongly wrote it pass unnoticed
    fresh = os.path.join(tmp, "fresh.log")
    open(fresh, "wb").write(b"a proof no earlier case stored\n")
    refused("stale with evidence", 2, "stale", "--run-id", "eeeeeeeeeeee", "--evidence", fresh)
    refused("audit with a note", 2, "audit", "why")
    # two runs share a second: the grade is refused as ambiguous, and its proof must not linger
    refused("an ambiguous run (same second), proof not left behind", 1,
            "good", "--ts", SAME, "--tag", "code-draft", "--evidence", fresh)

    # ---- audit: clean ---------------------------------------------------------------------
    rc, out = run(env, "audit")
    check(rc == 0 and "every proof holds" in out, "audit passes when every proof is intact")
    check("3 of 4 grades name evidence (2 of 3 good)" in out,
          "audit reports how many grades carry a proof: " + out.splitlines()[0])

    # ---- audit: lifecycle probes, each on its own copy ------------------------------------
    def mutated(name, mutate, expect):
        d = os.path.join(tmp, "case-" + name)
        os.makedirs(d)
        shutil.copy(ledger, d)
        shutil.copytree(evdir, os.path.join(d, "evidence"))
        mutate(d)
        rc, out = run(env_for(d), "audit")
        check(rc == 1 and expect in out and "Traceback" not in out,
              f"audit fails (exit 1) when {name}" + ("" if rc == 1 and expect in out else f"  [rc={rc}] {out.strip()[:140]}"))

    def proof_of(d, run_id):            # the file the grade on that run names
        return os.path.join(d, next(r["evidence_file"] for r in rows(os.path.join(d, "usage.jsonl"))
                                    if r.get("ref_run_id") == run_id))

    def delete_proof(d):
        os.remove(proof_of(d, R["bbbb"]))

    def edit_proof(d):                  # same length, different bytes: only a hash notices
        p = proof_of(d, R["aaaa"])
        data = open(p, "rb").read()
        edited = data.replace(b"12 passed", b"99 passed")
        assert edited != data and len(edited) == len(data), "the probe did not change the proof"
        open(p, "wb").write(edited)

    def repoint(value):
        def go(d):
            lp = os.path.join(d, "usage.jsonl")
            rs = rows(lp)
            for r in rs:
                if r.get("evidence_file"):
                    r["evidence_file"] = value
            write_ledger(lp, rs)
        return go

    def symlink_out(d):
        secret = os.path.join(d, "secret.txt")
        open(secret, "wb").write(proof_bytes)
        lp = os.path.join(d, "usage.jsonl")
        target = [r for r in rows(lp) if r.get("evidence_file")][0]["evidence_file"]
        full = os.path.join(d, target)
        os.remove(full)
        os.symlink(secret, full)

    mutated("a proof is deleted", delete_proof, "is missing")
    mutated("a proof is edited to the same length", edit_proof, "no longer matches")
    mutated("a ledger row points up out of the folder (../)", repoint("../usage.jsonl"), "outside")
    mutated("a ledger row points at an absolute path", repoint("/etc/hosts"), "outside")
    mutated("a ledger row's evidence_file is not a string", repoint(123), "not a path")
    mutated("a proof is a symlink to a file outside the folder", symlink_out, "outside")

# ---- an old-schema mirror is rebuilt from the ledger, nothing lost ----------------------------
with tempfile.TemporaryDirectory() as tmp:
    env = env_for(tmp)
    old = {"ts": ts(30), "script": "verdict", "verdict": "good", "note": "earlier", "tag": "research",
           "ref_ts": ts(40), "ref_model": "glm-5.3", "ref_script": "gemini", "ref_run_id": "oldoldoldold"}
    old2 = dict(old, ts=ts(29), note="earlier still", ref_run_id="older0000000")
    write_ledger(env["SMITH_LEDGER"], RUNS + [old, old2])
    c = sqlite3.connect(env["SMITH_VERDICT_DB"])           # the shape before 2026-11-07
    c.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, verdict TEXT NOT NULL, "
              "note TEXT, tag TEXT, ref_ts TEXT, ref_model TEXT, ref_script TEXT, ref_run_id TEXT)")
    c.execute("INSERT INTO verdicts (ts, verdict, note, tag, ref_ts, ref_model, ref_script, ref_run_id) "
              "VALUES (?,?,?,?,?,?,?,?)", tuple(old[k] for k in ("ts", "verdict", "note", "tag", "ref_ts",
                                                                  "ref_model", "ref_script", "ref_run_id")))
    c.commit()
    c.close()
    proof = os.path.join(tmp, "check.txt")
    open(proof, "wb").write(b"2326 chars in, 2326 chars out\n")
    rc, out = run(env, "good", "--run-id", R["aaaa"], "--tag", "doc-format", "--evidence", proof)
    c = sqlite3.connect(env["SMITH_VERDICT_DB"])
    cols = [r[1] for r in c.execute("PRAGMA table_info(verdicts)")]
    n = c.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    new = (c.execute("SELECT evidence_file FROM verdicts WHERE ref_run_id = ?", (R["aaaa"],)).fetchall()
           if "evidence_file" in cols else [])
    c.close()
    ledger_grades = sum(1 for r in rows(env["SMITH_LEDGER"]) if r.get("script") == "verdict")
    check(rc == 0 and "evidence_file" in cols, "an old-schema mirror gains the evidence columns")
    check(n == ledger_grades == 3 and len(new) == 1 and new[0][0],
          f"the rebuilt mirror holds every ledger grade once ({n} of {ledger_grades}), new one with its proof")

# ---- the folder is git-ignored: a proof can hold private output -------------------------------
gi = os.path.join(os.path.dirname(_HERE), ".gitignore")
check(os.path.isfile(gi) and "data/evidence/" in [l.strip() for l in open(gi)],
      ".gitignore lists data/evidence/")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
