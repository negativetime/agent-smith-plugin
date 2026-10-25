#!/usr/bin/env python3
"""How a grade counts: one rule, shared by every report that weighs a route.

A grade is a verdict line written by verdict.py (good, bad or stale). The rule:

  - every good or bad grade counts, including several grades on one run. A run row can
    stand for more than one answer (runs logged in the same second by the same model share
    a key), and a second grade on a run is a second judgment, not an overwrite
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


def run_key(r):
    """Identity of a run. ts has only seconds resolution, so script and model join it."""
    return (r.get("ts"), r.get("script"), r.get("model"))


def ref_key(v):
    """The run a grade names."""
    return (v.get("ref_ts"), v.get("ref_script"), v.get("ref_model"))


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
    return (grade.get("tag") or "untagged", run.get("model") or "?")


def routes(pairs, route=default_route):
    """({route: [(run ts, verdict), ...]}, {route: grades collapsed}) from judged() pairs:
    one entry per incident per route, in run order."""
    out, seen, collapsed = {}, set(), {}
    for r, v in pairs:
        k, inc = route(r, v), incident(v)
        if inc is not None and (k, inc) in seen:
            collapsed[k] = collapsed.get(k, 0) + 1
            continue
        if inc is not None:
            seen.add((k, inc))
        out.setdefault(k, []).append((r.get("ts") or "", v["verdict"]))
    return out, collapsed


def latest(grades):
    """{run key: last grade}, stale included. For a mark beside a run or the review
    queue, where the question is "has anyone looked", never for counting."""
    out = {}
    for v in grades:
        out[ref_key(v)] = v
    return out
