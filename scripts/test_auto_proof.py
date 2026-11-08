#!/usr/bin/env python3
"""Probes for the automatic proof verdict.py runs on a research or doc-format good. Pure stdlib,
no model calls, no requests: the proof tools are swapped for stand-ins (SMITH_PROOF_SCRIPTS_DIR)
that log how they were called and exit with the code a case asks for. Every case runs against a
temp SMITH_LEDGER.

Why this exists (2026-11-07): the spot-check rule needs 3 proofs among a route's last 10 goods,
and the proof tools existed, but a proof only happened when the grader remembered to run one.
doc-format @ gemini-pro-latest sat on a streak of 13 with 0 proofs. Each refusal case asserts the
exit code AND that nothing was written, because a refusal that still leaves a row or a proof
behind is a silent half-success.

    python3 -B scripts/test_auto_proof.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
all_pass = True
_ROOTS = []            # temp ledgers, removed at the end


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


STAND_IN = r'''
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": os.path.basename(__file__), "argv": sys.argv[1:]}) + "\n")
if os.environ.get("FAKE_OUT", "x") != "":
    print(f"REPORT {os.path.basename(__file__)} rc={os.environ.get('FAKE_RC', '0')}")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


class Case:
    """A temp ledger dir with stand-in proof tools beside it."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="autoproof-")
        _ROOTS.append(self.root)
        self.tools = os.path.join(self.root, "tools")
        os.makedirs(self.tools)
        for name in ("proof_urls.py", "proof_doc.py"):
            with open(os.path.join(self.tools, name), "w") as f:
                f.write(STAND_IN)
        self.ledger = os.path.join(self.root, "usage.jsonl")
        self.log = os.path.join(self.root, "calls.jsonl")
        self.n = 0
        open(self.ledger, "w").close()

    def add_run(self, tag, answer="<html>answer</html>\n", inputs=None, keep_output=True):
        """One run row; its answer saved under outputs/ as gemini.py does. inputs: list of
        (kind, name, role, data) to archive in <run>.in/, or raw row dicts (used as given)."""
        self.n += 1
        rid = f"{self.n:012d}"
        ts = f"2026-11-07T20:00:{self.n:02d}"
        rel = f"outputs/2026-11-07/{ts.replace(':', '_')}-gemini-m.txt"
        row = {"ts": ts, "run_id": rid, "script": "gemini", "model": "m", "purpose": "probe",
               "status": "ok"}
        if tag:
            row["tag"] = tag
        if keep_output:
            os.makedirs(os.path.join(self.root, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.root, rel), "w") as f:
                f.write(answer)
            row["output_file"] = rel
        if inputs is not None:
            rows = []
            for i, it in enumerate(inputs, 1):
                if isinstance(it, dict):
                    rows.append(it)
                    continue
                kind, name, role, data = it
                safe = "stdin.txt" if kind == "stdin" else f"file-{i}-{name}"
                arel = rel[:-4] + ".in/" + safe
                os.makedirs(os.path.join(self.root, os.path.dirname(arel)), exist_ok=True)
                with open(os.path.join(self.root, arel), "w") as f:
                    f.write(data)
                r = {"kind": kind, "name": name, "bytes": len(data), "sha256": "x", "archived": arel}
                if role:
                    r["role"] = role
                rows.append(r)
            row["inputs"] = rows
        with open(self.ledger, "a") as f:
            f.write(json.dumps(row) + "\n")
        return rid, rel

    def grade(self, *argv, rc=0, out=True, env_extra=None):
        # SMITH_VERDICT_DB too: verdict_db defaults to the REAL data/verdicts.db whatever
        # SMITH_LEDGER says (this file's first run wrote 40 probe rows there; rebuilt from the ledger).
        env = dict(os.environ, SMITH_LEDGER=self.ledger, SMITH_PROOF_SCRIPTS_DIR=self.tools,
                   SMITH_VERDICT_DB=os.path.join(self.root, "verdicts.db"),
                   FAKE_LOG=self.log, FAKE_RC=str(rc), FAKE_OUT="x" if out else "")
        env.pop("SMITH_AUTO_PROOF", None)
        env.update(env_extra or {})
        if os.path.exists(self.log):
            os.remove(self.log)
        p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "verdict.py"), *argv],
                           env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr

    def calls(self):
        if not os.path.exists(self.log):
            return []
        return [json.loads(l) for l in open(self.log) if l.strip()]

    def last_row(self):
        rows = [json.loads(l) for l in open(self.ledger) if l.strip()]
        return rows[-1]

    def snapshot(self):
        """Everything a refusal must leave alone: the ledger bytes and every file outside tools/."""
        files = []
        for d, _, fs in os.walk(self.root):
            if d.startswith(self.tools):
                continue
            files += [os.path.relpath(os.path.join(d, f), self.root) for f in fs if f != "calls.jsonl"]
        return open(self.ledger, "rb").read(), sorted(files)

    def evidence_text(self, row):
        return open(os.path.join(self.root, row["evidence_file"])).read()


# ---- research -----------------------------------------------------------------------------------
C = Case()
rid, rel = C.add_run("research")
rc, out = C.grade("good", "urls fine", "--run-id", rid, rc=0)
row, calls = C.last_row(), C.calls()
check(rc == 0 and row.get("verdict") == "good", "research good, check clean: graded good")
check(len(calls) == 1 and calls[0]["tool"] == "proof_urls.py"
      and calls[0]["argv"] == [os.path.join(C.root, rel)],
      "...proof_urls.py ran once, on that run's saved answer (absolute path), nothing else passed")
check(row.get("evidence_file") and C.evidence_text(row).startswith("REPORT proof_urls.py rc=0"),
      "...its report is attached as the proof")
check(row.get("evidence_auto") == "proof_urls.py", "...and the row says the proof was automatic")
check("proof: proof_urls.py passed" in out, "...stdout says a proof was attached")

C = Case()
rid, _ = C.add_run("research")
before = C.snapshot()
rc, out = C.grade("good", "urls fine", "--run-id", rid, rc=1)
check(rc == 1 and "REFUSED" in out and "REPORT proof_urls.py rc=1" in out,
      "research good, check found a missing URL: REFUSED, exit 1, the report is shown")
check(C.snapshot() == before, "...and nothing was written: ledger bytes, evidence, verdicts.db unchanged")
check("--no-proof" in out, "...the refusal names the way through (--no-proof) for link rot")

rc, out = C.grade("good", "link rot, read it", "--run-id", rid, "--no-proof", rc=1)
row = C.last_row()
check(rc == 0 and row.get("verdict") == "good" and "evidence_file" not in row and not C.calls(),
      "--no-proof: graded good, no proof, and the check never ran")

C = Case()
rid, _ = C.add_run("research")
rc, out = C.grade("good", "grounding only", "--run-id", rid, rc=3)
row = C.last_row()
check(rc == 0 and row.get("verdict") == "good" and "evidence_file" not in row,
      "research, only unverifiable URLs (exit 3): graded good WITHOUT a proof (a partial proof is not one)")
check("no proof attached: only unverifiable URLs" in out and "does not help this route" in out,
      "...and stdout says why, and what it costs")

rid, _ = C.add_run("research")
rc, out = C.grade("good", "no urls", "--run-id", rid, rc=2)
check(rc == 0 and "evidence_file" not in C.last_row() and "no URL in the answer" in out,
      "research, no URL (exit 2): graded good without a proof, says so")

rid, _ = C.add_run("research")
rc, out = C.grade("good", "empty report", "--run-id", rid, rc=0, out=False)
check(rc == 0 and "evidence_file" not in C.last_row(),
      "exit 0 with an EMPTY report is not a proof: nothing attached")

rid, _ = C.add_run("research")
proof = os.path.join(C.root, "mine.txt")
open(proof, "w").write("my own check\n")
rc, out = C.grade("good", "own proof", "--run-id", rid, "--evidence", proof, rc=1)
row = C.last_row()
check(rc == 0 and not C.calls() and C.evidence_text(row) == "my own check\n"
      and "evidence_auto" not in row,
      "--evidence given: the automatic check is skipped and the grader's proof is attached")

rid, _ = C.add_run("research")
rc, out = C.grade("bad", "invented a URL", "--run-id", rid, rc=0)
check(rc == 0 and not C.calls() and "evidence_file" not in C.last_row(),
      "a bad grade never runs the check")

rid, _ = C.add_run("research")
rc, out = C.grade("good", "off", "--run-id", rid, rc=1, env_extra={"SMITH_AUTO_PROOF": "0"})
check(rc == 0 and not C.calls(), "SMITH_AUTO_PROOF=0: the check never runs")

# ---- which shape: the grade's own tag wins, else the run's ---------------------------------------
C = Case()
rid, _ = C.add_run("classify")
rc, _ = C.grade("good", "fine", "--run-id", rid, rc=1)
check(rc == 0 and not C.calls(), "a classify good runs no check")
rid, _ = C.add_run("research")
rc, _ = C.grade("good", "fine", "--run-id", rid, rc=0)
check(C.calls() and C.calls()[0]["tool"] == "proof_urls.py",
      "no --tag on the grade: the run's tag (research) picks the check")
rid, _ = C.add_run("classify")
rc, _ = C.grade("good", "fine", "--run-id", rid, "--tag", "research", rc=0)
check(C.calls() and C.calls()[0]["tool"] == "proof_urls.py", "--tag research on a classify run: the grade's tag wins")
rid, _ = C.add_run("research")
rc, _ = C.grade("good", "fine", "--run-id", rid, "--tag", "classify", rc=1)
check(rc == 0 and not C.calls(), "--tag classify on a research run: no check")

rid, _ = C.add_run("research", keep_output=False)
rc, out = C.grade("good", "gone", "--run-id", rid, rc=1)
check(rc == 0 and not C.calls() and "no saved answer to check" in out,
      "no saved answer: graded good without a proof, the check never ran, says why")
rid, rel = C.add_run("research")
os.remove(os.path.join(C.root, rel))
rc, out = C.grade("good", "deleted", "--run-id", rid, rc=1)
check(rc == 0 and not C.calls() and "no saved answer to check" in out,
      "the row names a saved answer that was deleted since: same, the check never ran on a ghost path")

# ---- doc-format: finding the draft --------------------------------------------------------------
SHELL = "<!doctype html><style>x</style>"
DRAFT = "# Plan\n\nSpend $1,200.\n"


def source_of(call):
    a = call["argv"]
    return a[a.index("--source") + 1] if "--source" in a else None


C = Case()
rid, rel = C.add_run("doc-format", inputs=[("file", "html-doc-shell.html", None, SHELL),
                                           ("file", "draft.md", None, DRAFT)])
rc, out = C.grade("good", "parity", "--run-id", rid, rc=0)
c = C.calls()
check(rc == 0 and c and c[0]["tool"] == "proof_doc.py" and c[0]["argv"][0] == os.path.join(C.root, rel),
      "doc-format good: proof_doc.py runs on the saved page")
check(c and source_of(c[0]) == os.path.join(C.root, rel[:-4] + ".in/file-2-draft.md"),
      "...--source is the archived draft.md, never the .html shell")
check(C.last_row().get("evidence_auto") == "proof_doc.py", "...clean: attached, marked automatic")

rid, rel = C.add_run("doc-format", inputs=[("stdin", "stdin", "data", DRAFT)])
C.grade("good", "parity", "--run-id", rid, rc=0)
check(C.calls() and source_of(C.calls()[0]) == os.path.join(C.root, rel[:-4] + ".in/stdin.txt"),
      "stdin below a prompt argument (role data): stdin.txt is the draft")

rid, _ = C.add_run("doc-format", inputs=[("stdin", "stdin", "prompt", "Convert:\n" + DRAFT)])
rc, out = C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None,
      "stdin WAS the prompt (role prompt): not used as the draft, structure-only check")
check(rc == 0 and "evidence_file" not in C.last_row() and "came inside the prompt on stdin" in out,
      "...exit 3: graded good without a proof, and says the draft came inside the prompt")

rid, _ = C.add_run("doc-format", inputs=[("stdin", "stdin", "data", DRAFT + "\n" + SHELL)])
rc, out = C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None and "carries HTML" in out,
      "draft + shell piped together under a prompt (the gemini-cli lane): not the draft alone, never a false refusal")

rid, _ = C.add_run("doc-format", inputs=[("stdin", "stdin", None, DRAFT)])
C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None,
      "stdin with NO role (unknown): not trusted as the draft")

rid, _ = C.add_run("doc-format", inputs=[("file", "a.md", None, DRAFT), ("file", "b.md", None, DRAFT)])
rc, out = C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None and "2 archived inputs could be the draft" in out,
      "two possible drafts: neither is guessed, and it says so")

rid, _ = C.add_run("doc-format")
rc, out = C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None and "kept no copy of its input" in out,
      "a run with no archived input (pre 2026-11-07): structure only, says why")

rid, _ = C.add_run("doc-format", inputs=[{"kind": "file", "name": "x.md", "bytes": 1, "sha256": "x",
                                          "archived": "../../../../etc/passwd.md"}])
rc, out = C.grade("good", "parity", "--run-id", rid, rc=3)
check(C.calls() and source_of(C.calls()[0]) is None and "not in the outputs folder" in out,
      "an archived path that leaves the outputs folder (a hand-edited row) is never passed on")

rid, _ = C.add_run("doc-format", inputs=[("file", "draft.md", None, DRAFT)])
before = C.snapshot()
rc, out = C.grade("good", "parity", "--run-id", rid, rc=1)
check(rc == 1 and "REFUSED" in out and C.snapshot() == before,
      "doc-format check finds a problem: REFUSED, nothing written")

# ---- flag misuse -------------------------------------------------------------------------------
rc, out = C.grade("bad", "x", "--run-id", rid, "--no-proof")
check(rc == 2 and "--no-proof only" in out, "--no-proof on a bad: refused (exit 2)")
rc, out = C.grade("good", "x", "--run-id", rid, "--no-proof", "--evidence", proof)
check(rc == 2 and "--no-proof only" in out, "--no-proof with --evidence: refused (exit 2)")

for d in _ROOTS:
    shutil.rmtree(d, ignore_errors=True)
print("\nALL PASS" if all_pass else "\nSOME FAILED")
sys.exit(0 if all_pass else 1)
