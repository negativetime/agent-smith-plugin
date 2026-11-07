#!/usr/bin/env python3
"""Probes for the review tier: spot-check needs proofs in the shapes that can have one.

Decided 2026-11-07. On that day's ledger 2 of 78 routes sat at spot-check, and one of them,
research @ gemini-pro-latest, was 51 good / 12 bad incidents with a streak of exactly 10: the
shape that produced fabricated URLs, on notes alone. The rule: in research, doc-format,
code-draft and app-build a streak of 10 earns spot-check only if 3 of its last 10 goods carry
a proof that HOLDS NOW (file there, sha256 matching, inside evidence/). Everything else is as
before. A record that says a proof exists is not the proof, so every case that matters changes
the file on disk, not the ledger.

    python3 -B scripts/test_review_tier.py
"""
import hashlib
import json
import os
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


os.environ.pop("SMITH_EVIDENCE_NEEDED", None)


class Ledger:
    """A temp data/ folder with a ledger and an evidence/ folder, built row by row."""

    def __init__(self, tmp):
        self.root = tmp
        os.makedirs(os.path.join(tmp, "evidence"), exist_ok=True)
        self.path = os.path.join(tmp, "usage.jsonl")
        self.rows, self.n = [], 0

    def grade(self, tag, model, verdict="good", proof=None, note="", gtag="same"):
        """A run plus one grade on it. proof=bytes writes a real file and names it. gtag is the
        grade's own tag: "same" (default) copies the run's, None writes a grade with no tag."""
        self.n += 1
        ts = f"2026-11-01T10:{self.n // 60:02d}:{self.n % 60:02d}"
        rid = f"{self.n:012d}"
        self.rows.append({"ts": ts, "run_id": rid, "script": "gemini", "model": model, "tag": tag,
                          "purpose": "probe"})
        g = {"ts": ts, "script": "verdict", "verdict": verdict, "note": note,
             "ref_ts": ts, "ref_model": model, "ref_script": "gemini", "ref_run_id": rid}
        if gtag == "same":
            g["tag"] = tag
        elif gtag is not None:
            g["tag"] = gtag
        if proof is not None:
            sha = hashlib.sha256(proof).hexdigest()
            name = f"{sha[:16]}.txt"
            open(os.path.join(self.root, "evidence", name), "wb").write(proof)
            g.update(evidence_file=f"evidence/{name}", evidence_sha256=sha, evidence_bytes=len(proof))
        self.rows.append(g)
        return g

    def flush(self):
        with open(self.path, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in self.rows)

    def tier(self, tag, model):
        self.flush()
        runs, grs, _ = grades.read(self.path)
        full, _ = grades.route_grades(grades.judged(runs, grs))
        return grades.review_tier(full[(tag, model)], tag, self.root)


def streak_of(L, tag, model, n, proofs_at=()):
    """n goods in a row; the goods at the indexes in proofs_at carry a proof."""
    for i in range(n):
        L.grade(tag, model, proof=(f"proof {tag} {i}\n".encode() if i in proofs_at else None))


# ---- the rule, shape by shape -----------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 10)
    t = L.tier("research", "m")
    check(t["tier"] == "light" and t["held"] and t["proved"] == 0 and t["needed"] == 3,
          f"research, streak 10, no proof: held at light review ({t['proved']} of {t['needed']})")
    check("held below spot-check: 0 of 3 proofs" in grades.tier_label(t),
          "the label says WHY it is held, not just that it is lower")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "doc-format", "m", 10, proofs_at=(2, 5, 9))
    t = L.tier("doc-format", "m")
    check(t["tier"] == "spot" and not t["held"] and t["proved"] == 3,
          "doc-format, streak 10, 3 proofs that hold: spot-check")
    check(grades.tier_label(t) == "trusted-shape (spot-check only; 3 proofs in its last 10 goods)",
          "the spot-check label shows how many proofs back it")

for tag in ("code-draft", "app-build"):
    with tempfile.TemporaryDirectory() as tmp:
        L = Ledger(tmp)
        streak_of(L, tag, "m", 12)
        t = L.tier(tag, "m")
        check(t["held"], f"{tag} is gated too")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "translate", "m", 10)
    t = L.tier("translate", "m")
    check(t["tier"] == "spot" and t["needed"] == 0 and grades.tier_label(t) == "trusted-shape (spot-check only)",
          "translate (a judgement shape, no natural proof): spot-check on the streak alone, label unchanged")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 9)
    t = L.tier("research", "m")
    check(t["tier"] == "light" and not t["held"] and grades.tier_label(t) == "light review",
          "streak 9: light review as before, nothing is held (only the spot-check tier is gated)")
    L2 = Ledger(os.path.join(tmp, "b"))
    streak_of(L2, "research", "m", 4)
    check(L2.tier("research", "m")["tier"] == "full", "streak 4: full review as before")

# ---- a record that says a proof exists is not the proof ---------------------------------------
def three_proofs(tmp):
    L = Ledger(tmp)
    streak_of(L, "research", "m", 10, proofs_at=(1, 4, 8))
    return L


def stored(L, i):
    return os.path.join(L.root, [r for r in L.rows if r.get("evidence_file")][i]["evidence_file"])


with tempfile.TemporaryDirectory() as tmp:
    L = three_proofs(tmp)
    os.remove(stored(L, 1))
    t = L.tier("research", "m")
    check(t["tier"] == "light" and t["proved"] == 2, "one of three proofs deleted from disk: held (2 of 3)")

with tempfile.TemporaryDirectory() as tmp:
    L = three_proofs(tmp)
    open(stored(L, 0), "wb").write(b"edited after the grade\n")
    t = L.tier("research", "m")
    check(t["tier"] == "light" and t["proved"] == 2, "one proof edited after the grade: held (hash mismatch)")

with tempfile.TemporaryDirectory() as tmp:
    L = three_proofs(tmp)
    outside = os.path.join(tmp, "secret.txt")
    open(outside, "wb").write(b"not evidence\n")
    for r in L.rows:
        if r.get("evidence_file") and r["evidence_sha256"] == hashlib.sha256(open(stored(L, 2), "rb").read()).hexdigest():
            r["evidence_file"] = "../secret.txt"
            r["evidence_sha256"] = hashlib.sha256(b"not evidence\n").hexdigest()
    t = L.tier("research", "m")
    check(t["tier"] == "light" and t["proved"] == 2, "a ledger row pointing outside evidence/: not counted, not opened")

# ---- the window: the last 10 goods, in the CURRENT streak -------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 13, proofs_at=(0, 1, 2))      # proofs only in the 3 oldest goods
    t = L.tier("research", "m")
    check(t["streak"] == 13 and t["tier"] == "light" and t["proved"] == 0,
          "streak 13 with proofs only on its oldest 3 goods: they fall outside the last 10, held")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 10, proofs_at=(0, 1, 2))
    L.grade("research", "m", verdict="bad", note="fabricated url")
    streak_of(L, "research", "m", 4)
    t = L.tier("research", "m")
    check(t["streak"] == 4 and t["tier"] == "full", "a bad resets the streak; earlier proofs do not carry over")

# ---- the switch --------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 10)
    os.environ["SMITH_EVIDENCE_NEEDED"] = "0"
    check(L.tier("research", "m")["tier"] == "spot", "SMITH_EVIDENCE_NEEDED=0 switches the rule off")
    os.environ["SMITH_EVIDENCE_NEEDED"] = "1"
    check(L.tier("research", "m")["held"], "SMITH_EVIDENCE_NEEDED=1 with no proof: still held")
    os.environ["SMITH_EVIDENCE_NEEDED"] = "banana"
    t = L.tier("research", "m")
    check(t["needed"] == 3 and t["held"], "a malformed value falls back to 3, never to off")
    os.environ.pop("SMITH_EVIDENCE_NEEDED")

# ---- two grades with one note: the proof behind the second is not lost -------------------------
with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    L.grade("research", "m", note="urls ok")
    L.grade("research", "m", note="urls ok", proof=b"3 of 3 resolved\n")
    L.flush()
    runs, grs, _ = grades.read(L.path)
    full, collapsed = grades.route_grades(grades.judged(runs, grs))
    seq = full[("research", "m")]
    check(len(seq) == 1 and collapsed[("research", "m")] == 1 and seq[0][1].get("evidence_file"),
          "same note twice collapses to one event, and the one kept is the one with the proof")

# ---- a grade written without --tag takes its run's tag -----------------------------------------
def route_keys(L):
    L.flush()
    runs, grs, _ = grades.read(L.path)
    full, _ = grades.route_grades(grades.judged(runs, grs))
    return full


with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    for _ in range(3):
        L.grade("research", "m", gtag=None)
    full = route_keys(L)
    check(len(full.get(("research", "m"), [])) == 3,
          "three untagged goods on a research run count toward research @ m")
    check(("untagged", "m") not in full, "...and not toward an 'untagged' route")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "m", 9, proofs_at=(1, 4, 8))
    L.grade("research", "m", verdict="bad", note="fabricated url", gtag=None)      # the hole: a bad with no --tag
    L.grade("research", "m")
    t = L.tier("research", "m")
    check(t["streak"] == 1 and t["tier"] == "full",
          "a bad graded WITHOUT --tag resets its real route's streak (it used to vanish into 'untagged')")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    L.grade("classify", "m", gtag="research")
    full = route_keys(L)
    check(("research", "m") in full and ("classify", "m") not in full,
          "a grade's own --tag wins over its run's tag")

with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    L.rows.append({"ts": "2026-11-01T10:00:01", "run_id": "x" * 12, "script": "gemini", "model": "m", "purpose": "probe"})
    L.rows.append({"ts": "2026-11-01T10:00:01", "script": "verdict", "verdict": "good", "note": "",
                   "ref_ts": "2026-11-01T10:00:01", "ref_model": "m", "ref_script": "gemini", "ref_run_id": "x" * 12})
    check(("untagged", "m") in route_keys(L), "no tag on the grade or the run: 'untagged', as before")

# ---- routes() is unchanged by the refactor -----------------------------------------------------
def old_routes(pairs, route=grades.default_route):
    out, seen, collapsed = {}, set(), {}
    for r, v in pairs:
        k, inc = route(r, v), grades.incident(v)
        if inc is not None and (k, inc) in seen:
            collapsed[k] = collapsed.get(k, 0) + 1
            continue
        if inc is not None:
            seen.add((k, inc))
        out.setdefault(k, []).append((r.get("ts") or "", v["verdict"]))
    return out, collapsed


with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    for i in range(40):
        L.grade(["research", "doc-format", "translate"][i % 3], ["a", "b"][i % 2],
                verdict="bad" if i % 7 == 0 else "good", note=["", "same", "other"][i % 3],
                proof=b"p%d\n" % i if i % 5 == 0 else None)
    L.flush()
    runs, grs, _ = grades.read(L.path)
    pairs = grades.judged(runs, grs)
    check(grades.routes(pairs) == old_routes(pairs), "routes() returns exactly what it did before the refactor (40 mixed grades)")

# ---- end to end: what the report prints --------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    L = Ledger(tmp)
    streak_of(L, "research", "no-proof-model", 10)
    streak_of(L, "doc-format", "proved-model", 10, proofs_at=(0, 3, 7))
    streak_of(L, "translate", "judgement-model", 10)
    L.flush()
    env = dict(os.environ, SMITH_LEDGER=L.path, SMITH_VERDICT_DB=os.path.join(tmp, "v.db"))
    out = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "usage_report.py")], env=env,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    line = lambda m: next((l for l in out.splitlines() if f"@ {m}:" in l), "")      # the routing-weights line
    check("streak 10 -> light review (held below spot-check: 0 of 3 proofs" in line("no-proof-model"),
          "usage_report prints the held route with its reason: " + line("no-proof-model").strip()[:110])
    check("streak 10 -> trusted-shape (spot-check only; 3 proofs in its last 10 goods)" in line("proved-model"),
          "usage_report prints the proved route at spot-check")
    check(line("judgement-model").rstrip().endswith("streak 10 -> trusted-shape (spot-check only)"),
          "usage_report prints the judgement-shape route exactly as before")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
