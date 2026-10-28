#!/usr/bin/env python3
"""SQLite mirror of the verdict trail in usage.jsonl.

usage.jsonl stays the source of truth (gap_report.py and verdict.py read it
directly). This gives anything that isn't a Claude-side JSONL parser — sqlite3
on the CLI, a teammate's script, a non-Python tool — a queryable, parameterized
table of the same verdicts:

    sqlite3 data/verdicts.db "select tag, verdict, count(*) from verdicts \
        group by tag, verdict order by tag"

Rebuild it from the ledger any time it drifts (e.g. after hand-editing
usage.jsonl):

    python3 verdict_db.py rebuild
"""
import json
import os
import sqlite3
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.environ.get("SMITH_LEDGER") or os.path.join(SKILL, "data", "usage.jsonl")
DB = os.environ.get("SMITH_VERDICT_DB") or os.path.join(SKILL, "data", "verdicts.db")

# Before 2026-10-25 ts was the PRIMARY KEY, and INSERT OR REPLACE let a grade written in
# the same second as another overwrite it: the mirror held 328 of the ledger's 411 grades.
# Rows are keyed by id now, and connect() rebuilds a mirror still on an old schema (no id, or
# no ref_run_id: grades name a run by its run_id since 2026-10-28).
SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  verdict     TEXT NOT NULL,
  note        TEXT,
  tag         TEXT,
  ref_ts      TEXT,
  ref_model   TEXT,
  ref_script  TEXT,
  ref_run_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_tag ON verdicts(tag);
CREATE INDEX IF NOT EXISTS idx_verdicts_verdict ON verdicts(verdict);
"""


COLS = ("ts", "verdict", "note", "tag", "ref_ts", "ref_model", "ref_script", "ref_run_id")
_MARKS = ", ".join("?" for _ in COLS)


def _values(rec):
    return tuple(rec.get(c) for c in COLS)


def connect(path=None):
    conn = sqlite3.connect(path or DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(verdicts)")]
    stale = bool(cols) and ("id" not in cols or "ref_run_id" not in cols)
    if stale:
        conn.execute("DROP TABLE verdicts")
    conn.executescript(SCHEMA)
    if stale:
        _fill(conn)
    return conn


def insert(rec, path=None):
    """Add one verdict record (dict, verdict.py's shape) unless that exact grade is already
    there, which it is when connect() has just rebuilt the mirror from the ledger."""
    conn = connect(path)
    with conn:
        conn.execute(
            f"""INSERT INTO verdicts ({", ".join(COLS)})
                SELECT {_MARKS}
                WHERE NOT EXISTS (SELECT 1 FROM verdicts WHERE {" AND ".join(f"{c} IS ?" for c in COLS)})""",
            _values(rec) + _values(rec),
        )
    conn.close()


def _fill(conn):
    if not os.path.isfile(LEDGER):
        return
    with conn, open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("script") == "verdict":
                conn.execute(f"INSERT INTO verdicts ({', '.join(COLS)}) VALUES ({_MARKS})",
                             _values(r))


def rebuild(path=None):
    """Wipe and repopulate from usage.jsonl — the ledger is authoritative."""
    conn = connect(path)
    with conn:
        conn.execute("DELETE FROM verdicts")
    _fill(conn)
    n = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]
    conn.close()
    return n


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
        n = rebuild()
        print(f"rebuilt {DB} from {LEDGER}: {n} verdict(s)")
    else:
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 1 else 2)
