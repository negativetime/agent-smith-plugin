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

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
  ts          TEXT PRIMARY KEY,
  verdict     TEXT NOT NULL,
  note        TEXT,
  tag         TEXT,
  ref_ts      TEXT,
  ref_model   TEXT,
  ref_script  TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_tag ON verdicts(tag);
CREATE INDEX IF NOT EXISTS idx_verdicts_verdict ON verdicts(verdict);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB)
    conn.executescript(SCHEMA)
    return conn


def insert(rec, path=None):
    """Parameterized insert/replace of one verdict record (dict, verdict.py's shape)."""
    conn = connect(path)
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO verdicts
               (ts, verdict, note, tag, ref_ts, ref_model, ref_script)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rec.get("ts"), rec.get("verdict"), rec.get("note"), rec.get("tag"),
             rec.get("ref_ts"), rec.get("ref_model"), rec.get("ref_script")),
        )
    conn.close()


def rebuild(path=None):
    """Wipe and repopulate from usage.jsonl — the ledger is authoritative."""
    conn = connect(path)
    with conn:
        conn.execute("DELETE FROM verdicts")
        if os.path.isfile(LEDGER):
            with open(LEDGER) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("script") == "verdict":
                        conn.execute(
                            """INSERT OR REPLACE INTO verdicts
                               (ts, verdict, note, tag, ref_ts, ref_model, ref_script)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (r.get("ts"), r.get("verdict"), r.get("note"), r.get("tag"),
                             r.get("ref_ts"), r.get("ref_model"), r.get("ref_script")),
                        )
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
