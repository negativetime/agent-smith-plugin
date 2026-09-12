#!/usr/bin/env python3
"""Guard against the dead-setting bug class: a knob that is written but never read.

Prompted 2026-09-12 by claude-mem, where the observer's own error message told users to
raise `CLAUDE_MEM_LLM_TIMEOUT_MS`, that name was only ever read from `process.env`, and its
near-identical sibling `CLAUDE_MEM_API_TIMEOUT_MS` WAS a settings.json key. Setting it in
the obvious place did nothing, silently, for everyone (thedotmack/claude-mem#4065).

A setting that is accepted and ignored is invisible. Every check here is one direction of
"declared and consumed must agree":

  1. every CLI flag is actually read as args.<dest>       (a flag nothing consumes)
  2. every SMITH_* env var the docs promise is read        (the claude-mem bug exactly)
  3. every SMITH_* env var the code reads is documented    (an undiscoverable knob)
  4. every smith_agent contract key reaches a real dest    (hand-written dict, so typo-prone)

Pure stdlib, no model calls.
"""
import importlib.util
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL = os.path.dirname(_HERE)
# Glob, never a hand-written list. The first run of this guard "found" two dead vars that
# were perfectly alive in research_nudge.py, which the list had simply omitted — the same
# declared-vs-consumed mismatch the guard exists to catch, one level up in the guard itself.
SCRIPTS = tuple(sorted(f for f in os.listdir(_HERE)
                       if f.endswith(".py") and not f.startswith("test_")))

all_pass = True


def check(ok, what):
    global all_pass
    print(("PASS  " if ok else "FAIL  ") + what)
    all_pass = all_pass and bool(ok)


def read(p):
    return open(p, encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- 1. dead CLI flags
FLAG_RE = re.compile(r'add_argument\(\s*("--[^"]+"|\'--[^\']+\')([^)]*)', re.S)

for fn in ("gemini.py", "smith_agent.py"):
    src = read(os.path.join(_HERE, fn))
    dead = []
    for m in FLAG_RE.finditer(src):
        flag = m.group(1).strip("\"'")
        d = re.search(r'dest\s*=\s*["\']([^"\']+)', m.group(2))
        dest = d.group(1) if d else flag.lstrip("-").replace("-", "_")
        # read as args.<dest>, or pulled by name (getattr / a key in a dict of settings)
        if (re.search(rf"\bargs\.{re.escape(dest)}\b", src)
                or re.search(rf'["\']{re.escape(dest)}["\']', src)):
            continue
        dead.append(flag)
    check(not dead, f"{fn}: every CLI flag is read ({', '.join(dead) or 'none dead'})")

# ---------------------------------------------------------------- env vars, both ways
code = "\n".join(read(os.path.join(_HERE, f)) for f in SCRIPTS)
docs = read(os.path.join(_SKILL, "SKILL.md"))
for ref in ("playbooks.md", "measured-results.md"):
    p = os.path.join(_SKILL, "references", ref)
    if os.path.exists(p):
        docs += read(p)

read_vars = set(re.findall(r'environ(?:\.get\(|\[)["\'](SMITH_[A-Z_]+)', code))
doc_vars = set(re.findall(r"\b(SMITH_[A-Z_]{2,})\b", docs))

# 2. promised but never read — the claude-mem failure
promised_dead = sorted(doc_vars - read_vars)
check(not promised_dead,
      f"every SMITH_* env var the docs promise is read by code ({', '.join(promised_dead) or 'none dead'})")

# 3. read but undocumented — a knob nobody can find
check(read_vars, "found SMITH_* env vars in the code at all (guard is not vacuous)")
undocumented = sorted(read_vars - doc_vars)
print(f"      note: {len(undocumented)} read-but-undocumented ({', '.join(undocumented) or 'none'})")

# ---------------------------------------------------------------- 4. contract keys
spec = importlib.util.spec_from_file_location("smith_agent",
                                              os.path.join(_HERE, "smith_agent.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

sa_src = read(os.path.join(_HERE, "smith_agent.py"))
dests = set()
for m in FLAG_RE.finditer(sa_src):
    flag = m.group(1).strip("\"'")
    d = re.search(r'dest\s*=\s*["\']([^"\']+)', m.group(2))
    dests.add(d.group(1) if d else flag.lstrip("-").replace("-", "_"))

# `verify` is consumed directly (appended to the task), not via an argparse dest.
SPECIAL = {"verify"}
orphans = sorted(k for k in sm.CONTRACT_KEYS if k not in dests and k not in SPECIAL)
check(not orphans,
      f"every contract key maps to a real flag or is special-cased ({', '.join(orphans) or 'none orphaned'})")

for k in SPECIAL:
    check(re.search(rf'meta(?:\.get\(|\[)["\']{k}["\']', sa_src) is not None,
          f"special-cased contract key {k!r} is actually consumed")

# and the reverse: a flag a contract cannot set is fine, but an UNKNOWN key must be refused
try:
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "c.md")
    open(p, "w").write("---\nmodel: m\nnonsense_key: 1\n---\nbody\n")
    sm.parse_contract(p)
    check(False, "an unknown contract key is refused, not ignored")
except sm.RepoError as exc:
    check("nonsense_key" in str(exc), "an unknown contract key is refused, not ignored")

sys.exit(0 if all_pass else 1)
