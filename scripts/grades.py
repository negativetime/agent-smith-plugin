#!/usr/bin/env python3
"""How a grade counts: one rule, shared by every report that weighs a route.

A grade is a verdict line written by verdict.py (good, bad or stale). The rule:

  - every good or bad grade counts, including several grades on one run. A run row written
    before 2026-10-28 can stand for more than one answer (runs logged in the same second by
    the same model share a key; newer rows carry a run_id), and a second grade on a run is a
    second judgment, not an overwrite
  - within a route (tag, model) one incident counts once, in run order: one bug graded on
    several runs is one event (20 translate rows with one note, found 2026-08-15)
  - stale is never good or bad, and a grade marked duplicate_of is the same judgment
    written twice, so it is skipped

Until 2026-10-25 usage_report.py and gap_report.py kept only the last grade on each run,
so every earlier grade on a run that carried several was dropped without a word. On that
day's ledger that hid 87 of 335 counted incidents, most of them grades verdict.py had filed
on one stale run before its stale-target guard existed. Those grades were moved to the runs
they graded (each keeps a moved_from field); this module stops the rest being dropped.
"""
import hashlib
import json
import os


def _tuple(r):
    return (r.get("ts"), r.get("script"), r.get("model"))


def run_key(r):
    """Identity of a run: its run_id. Rows written before 2026-10-28 have none, so for them
    it is ts with script and model, and ts has only seconds resolution: two runs of one model
    that finished in the same second share that key, and nothing can tell them apart."""
    return r.get("run_id") or _tuple(r)


def ref_key(v):
    """The run a grade names: its ref_run_id, or for older grades (ts, script, model)."""
    return v.get("ref_run_id") or (v.get("ref_ts"), v.get("ref_script"), v.get("ref_model"))


def grade_of(latest_grades, r):
    """The grade latest() holds for run r. A grade that names r's run_id; failing that, one
    written without a run id that names r by (ts, script, model). A grade on a sibling that
    finished in the same second names the sibling's id, so it is never r's."""
    return latest_grades.get(run_key(r)) or latest_grades.get(_tuple(r))


def incident(v):
    """The grade's incident id; for grades older than the field, a hash of the note."""
    if v.get("incident"):
        return v["incident"]
    note = " ".join((v.get("note") or "").split()).casefold()
    return hashlib.sha1(note.encode()).hexdigest()[:10] if note else None


def read(path):
    """(runs, grades, witnesses) from a ledger, in file order. Unreadable lines are skipped."""
    runs, grades, witnesses = [], [], []
    if not os.path.isfile(path):
        return runs, grades, witnesses
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            if r.get("script") == "verdict":
                grades.append(r)
            elif r.get("script") == "witness":
                witnesses.append(r)
            else:
                runs.append(r)
    return runs, grades, witnesses


def judged(runs, grades):
    """[(run, grade)] for every good or bad grade that names one of `runs`, in run order
    and then grade order. Duplicates and stale grades are left out."""
    by_key = {}
    for r in runs:
        by_key.setdefault(run_key(r), r)
        by_key.setdefault(_tuple(r), r)     # a grade written without a run id still finds it
    out = []
    for v in grades:
        if v.get("verdict") not in ("good", "bad") or v.get("duplicate_of"):
            continue
        r = by_key.get(ref_key(v))
        if r is not None:
            out.append((r, v))
    out.sort(key=lambda rv: (rv[0].get("ts") or "", rv[1].get("ts") or ""))
    return out


def default_route(run, grade):
    """(shape, model) a grade counts toward. A grade's own --tag wins; one written without it
    takes the tag of the run it grades, which every run carries (an untagged call exits 2).
    Until 2026-11-07 it fell to "untagged", so a bad graded without --tag could not reset its
    real route's streak. Measured that day: 76 judged grades had no tag of their own, 71 named
    a tagged run (70 of them classify); none of the 2 untagged bads was hiding, but nothing
    prevented it."""
    return (grade.get("tag") or run.get("tag") or "untagged", run.get("model") or "?")


def route_grades(pairs, route=default_route):
    """({route: [(run ts, grade), ...]}, {route: grades collapsed}) from judged() pairs: one
    entry per incident per route, in run order. A repeat of an incident already held is
    collapsed; if the grade held names no proof and the repeat (same verdict) does, the repeat
    takes its place in line, so writing the same note twice never loses the file behind the second."""
    out, seen, collapsed = {}, {}, {}
    for r, v in pairs:
        k, inc = route(r, v), incident(v)
        if inc is not None and (k, inc) in seen:
            collapsed[k] = collapsed.get(k, 0) + 1
            i = seen[(k, inc)]
            ts, held = out[k][i]
            if (not held.get("evidence_file") and v.get("evidence_file")
                    and v.get("verdict") == held.get("verdict")):
                out[k][i] = (ts, v)
            continue
        if inc is not None:
            seen[(k, inc)] = len(out.get(k, []))
        out.setdefault(k, []).append((r.get("ts") or "", v))
    return out, collapsed


def routes(pairs, route=default_route):
    """({route: [(run ts, verdict), ...]}, {route: grades collapsed}) from judged() pairs:
    one entry per incident per route, in run order."""
    full, collapsed = route_grades(pairs, route)
    return {k: [(ts, v["verdict"]) for ts, v in seq] for k, seq in full.items()}, collapsed


# ---- review tier ------------------------------------------------------------------------------
# A route's tier says how hard to read its next output: a streak of 5 goods earns light review,
# 10 earns spot-check only. Spot-check is where Claude stops reading most outputs, so it is the
# one tier where a "good" that is only a note is not enough. In the shapes below a mechanical
# proof exists (a test log, a resolved-URL list, a length or diff check) and a wrong good has
# already cost something (research: fabricated URLs, 12 bad incidents; doc-format: a house CSS
# block trimmed from 2326 to 2040 chars that still rendered), a streak of 10 reaches spot-check
# only if PROOFS_NEEDED of its last 10 goods carry a proof that holds NOW. Judgement shapes
# (translate, copy-draft, long-digest, ...) have no natural proof, so demanding one would only
# produce junk files; they are untouched. Decided 2026-11-07. SMITH_EVIDENCE_NEEDED=0 turns it off.
LIGHT_AT, SPOT_AT = 5, 10
PROOF_SHAPES = frozenset(("research", "doc-format", "code-draft", "app-build"))
PROOFS_NEEDED = 3


def proofs_needed():
    """PROOFS_NEEDED, or SMITH_EVIDENCE_NEEDED if set. A value that is not a whole number
    falls back to the default (the stricter side), never to off."""
    try:
        return max(0, int(os.environ["SMITH_EVIDENCE_NEEDED"]))
    except (KeyError, ValueError):
        return PROOFS_NEEDED


def proof_state(v, root):
    """(holds, why) for the proof a grade names. `root` is the folder that holds the ledger
    and its evidence/ folder. Re-opens the file every time: a record that says a proof exists
    is not the proof. A path outside root/evidence/ is never opened (a hand-edited ledger)."""
    rel = v.get("evidence_file")
    if not rel:
        return False, "no proof"
    if not isinstance(rel, str):
        return False, "evidence_file is not a path"
    base = os.path.realpath(os.path.join(root, "evidence"))
    path = os.path.realpath(os.path.join(root, rel))
    if os.path.dirname(path) != base:
        return False, f"{rel} is outside {base}; not opened"
    if not os.path.isfile(path):
        return False, f"{rel} is missing"
    with open(path, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != v.get("evidence_sha256"):
            return False, f"{rel} no longer matches its recorded sha256"
    return True, ""


def review_tier(seq, tag, root):
    """Tier for one route. `seq` is its [(run ts, grade)] in run order (route_grades); `tag`
    its shape. Returns {streak, tier: full|light|spot, proved, needed, held}: `held` is True
    when the streak is long enough for spot-check but the proofs are short, so the route sits
    at light review and the report can say why instead of quietly demoting it."""
    streak, window = 0, []
    for _, v in reversed(seq):
        if v.get("verdict") != "good":
            break
        streak += 1
        if len(window) < SPOT_AT:
            window.append(v)
    needed = proofs_needed() if tag in PROOF_SHAPES else 0
    proved = sum(1 for v in window if proof_state(v, root)[0]) if needed else 0
    if streak >= SPOT_AT and proved >= needed:
        tier = "spot"
    elif streak >= LIGHT_AT:
        tier = "light"
    else:
        tier = "full"
    return {"streak": streak, "tier": tier, "proved": proved, "needed": needed,
            "held": streak >= SPOT_AT and tier == "light"}


def tier_label(t):
    """The words the report prints for review_tier()'s result."""
    if t["held"]:
        return (f"light review (held below spot-check: {t['proved']} of {t['needed']} proofs "
                f"in its last {SPOT_AT} goods; attach one with verdict.py good --evidence FILE)")
    if t["tier"] == "spot":
        return ("trusted-shape (spot-check only"
                + (f"; {t['proved']} proofs in its last {SPOT_AT} goods)" if t["needed"] else ")"))
    return "light review" if t["tier"] == "light" else "full review"


def latest(grades):
    """{run key: last grade}, stale included. For a mark beside a run or the review
    queue, where the question is "has anyone looked", never for counting. Look a run up
    with grade_of(), which also finds a grade written without a run id."""
    out = {}
    for v in grades:
        out[ref_key(v)] = v
    return out
