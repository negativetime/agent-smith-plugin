#!/usr/bin/env python3
"""Probes for gemini.py input archiving: keep what a run was GIVEN beside what it said.

Why (2026-11-07): proof_doc.py --source DRAFT can back a doc-format grade with a content-parity check,
but only while the draft exists. Of the 10 goods in the window, 8 drafts were gone (4 deleted, 4 piped on
stdin and never saved) and 2 had changed since. The output was archived; its input never was.

Pure stdlib: backends are stubbed; no model, no network.   python3 -B scripts/test_input_archive.py
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gm)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


def sha(b):
    return hashlib.sha256(b).hexdigest()


ENV_KEYS = ("SMITH_LEDGER", "SMITH_NO_ARCHIVE", "SMITH_ARCHIVE_INPUTS", "SMITH_JSON_TRIM",
            "GEMINI_API_KEY", "ZAI_API_KEY")


def run(stdin="", files=(), env=None, canned="the answer", site="openai", flags=(), tmp=None,
        prompt="convert this to html"):
    """One main() call with a stubbed backend. Returns (row, tmp, stderr, tmpdir for the inputs)."""
    tmp = tmp or tempfile.mkdtemp(prefix="ia-")
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    os.environ["SMITH_LEDGER"] = os.path.join(tmp, "usage.jsonl")
    for k in ENV_KEYS[1:]:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    old = (gm.call_openai_compat, gm.http_json, gm.make_file_part, sys.argv, sys.stdin)
    file_flags = [x for f in files for x in ("--file", f)]
    if site == "openai":
        gm.call_openai_compat = lambda *a, **k: canned
        argv = ["gemini.py", "--backend", "openai", "--model", "stub-model", "--tag", "smoke",
                *flags, *([prompt] if prompt is not None else [])]
    else:
        os.environ["GEMINI_API_KEY"] = "k"
        gm.http_json = lambda *a, **k: {"candidates": [{"content": {"parts": [{"text": canned}]},
                                                        "finishReason": "STOP"}], "usageMetadata": {}}
        gm.make_file_part = lambda fp, key: {"text": "stub part"}
        argv = ["gemini.py", "--tag", "smoke", *flags, *file_flags, *([prompt] if prompt is not None else [])]
    sys.argv, sys.stdin = argv, io.StringIO(stdin)
    so, se = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            try:
                gm.main()
            except SystemExit:
                pass
    finally:
        gm.call_openai_compat, gm.http_json, gm.make_file_part, sys.argv, sys.stdin = old
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rows = [json.loads(l) for l in open(os.path.join(tmp, "usage.jsonl")) if l.strip()]
    row = [r for r in rows if r.get("script") == "gemini"][-1]
    return row, tmp, se.getvalue()


def read_bytes(tmp, rel):
    with open(os.path.join(tmp, rel), "rb") as f:
        return f.read()


def ledger_call(tmp, rec, output, env=None):
    """Call _ledger() directly against a ledger in tmp."""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    os.environ["SMITH_LEDGER"] = os.path.join(tmp, "usage.jsonl")
    for k in ENV_KEYS[1:]:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            gm._ledger(rec, output=output)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return [json.loads(l) for l in open(os.path.join(tmp, "usage.jsonl")) if l.strip()]


def mkfile(d, name, data):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    return p


def outputs_listing(tmp):
    out = []
    for dp, dn, fn in os.walk(os.path.join(tmp, "outputs")):
        for n in dn + fn:
            out.append(os.path.relpath(os.path.join(dp, n), tmp))
    return sorted(out)


DRAFT = "# Plan\n\nSpend $1,200 on TarotCore by March 3.\n\n## Risks\n\nNone known.\n"
fdir = tempfile.mkdtemp(prefix="ia-files-")

# ---- piped stdin -----------------------------------------------------------------------------------
row, tmp, se = run(stdin=DRAFT)
inp = row.get("inputs") or []
check(len(inp) == 1 and inp[0]["kind"] == "stdin" and inp[0]["bytes"] == len(DRAFT.encode())
      and inp[0]["sha256"] == sha(DRAFT.encode()), "piped stdin: one input, kind/bytes/sha256 recorded")
check(inp and inp[0].get("archived") == row["output_file"][:-4] + ".in/stdin.txt",
      "...archived beside the output as <run>.in/stdin.txt")
check(inp and read_bytes(tmp, inp[0]["archived"]) == DRAFT.encode(), "...and the copy is the draft byte for byte")
check(row["prompt_chars"] == len("convert this to html\n\n" + DRAFT), "...prompt_chars is unchanged by archiving")
check("[inputs] kept 1 input copy" in se, "...stderr says a copy was kept, and how to stop it")
check("SMITH_NO_ARCHIVE=1" in se and "SMITH_ARCHIVE_INPUTS=0" in se, "...naming both switches")

# stdin IS the whole prompt (no prompt argument): the copy is that prompt, instructions included
row, tmp, se = run(stdin="Convert to html:\n" + DRAFT, prompt=None)
i0 = (row.get("inputs") or [{}])[0]
check(i0.get("archived") and read_bytes(tmp, i0["archived"]) == ("Convert to html:\n" + DRAFT).encode()
      and row["prompt_chars"] == len("Convert to html:\n" + DRAFT),
      "no prompt argument: stdin is the whole prompt and is kept as sent (instructions and all)")

row, tmp, se = run()
check("inputs" not in row and not any(p.endswith(".in") for p in outputs_listing(tmp)),
      "no stdin and no files: no `inputs` field and no .in folder")
check("[inputs]" not in se, "...and stderr says nothing")
row, tmp, se = run(stdin="  \n\n ")
check("inputs" not in row, "whitespace-only stdin: nothing to keep")

# ---- --file ----------------------------------------------------------------------------------------
p1 = mkfile(fdir, "draft.md", DRAFT)
row, tmp, se = run(files=[p1], site="gemini")
inp = row.get("inputs") or []
check(len(inp) == 1 and inp[0]["kind"] == "file" and inp[0]["name"] == "draft.md"
      and inp[0]["sha256"] == sha(DRAFT.encode()) and read_bytes(tmp, inp[0]["archived"]) == DRAFT.encode(),
      "a text --file: recorded by basename and copied byte for byte")
check(inp and inp[0]["archived"].endswith(".in/file-1-draft.md"), "...as file-1-draft.md")
check(row.get("files") == 1 and row.get("file_names") == ["draft.md"],
      "...the existing `files` (a count here) and `file_names` fields are unchanged")

p2 = mkfile(fdir, "a.md", "AAA\n")
p3 = mkfile(fdir, "b.csv", "x,y\n1,2\n")
row, tmp, se = run(stdin=DRAFT, files=[p2, p3], site="gemini")
inp = row.get("inputs") or []
check([i["kind"] for i in inp] == ["stdin", "file", "file"], "stdin + two files: kept in the order given")
check([os.path.basename(i["archived"]) for i in inp] == ["stdin.txt", "file-2-a.md", "file-3-b.csv"],
      "...numbered so two files with one name could never collide")
check("[inputs] kept 3 input copies" in se, "...stderr counts them")

# ---- not copied: recorded with a reason --------------------------------------------------------------
pdf = b"%PDF-1.7\n\x00\x01\xff binary body"
p = mkfile(fdir, "report.pdf", pdf)
row, tmp, se = run(files=[p], site="gemini")
i0 = (row.get("inputs") or [{}])[0]
check(i0.get("why") == "binary" and "archived" not in i0 and i0.get("sha256") == sha(pdf) and i0.get("bytes") == len(pdf),
      "a binary file (NUL byte): recorded with size and sha256, NOT copied")
check(not any(x.endswith(".in") for x in outputs_listing(tmp)), "...and no .in folder is made for it")
check("[inputs]" not in se, "...and stderr does not claim a copy")

p = mkfile(fdir, "nul.dat", b"looks like text\x00but has a NUL byte")     # valid UTF-8, so only the NUL check catches it
row, tmp, se = run(files=[p], site="gemini")
check((row.get("inputs") or [{}])[0].get("why") == "binary" and "archived" not in (row.get("inputs") or [{}])[0],
      "valid UTF-8 with an embedded NUL: binary, not copied")

p = mkfile(fdir, "latin.txt", b"caf\xe9 \xff\xfe no nul bytes but not utf-8")
row, tmp, se = run(files=[p], site="gemini")
check((row.get("inputs") or [{}])[0].get("why") == "binary", "bytes that are not UTF-8 (no NUL): also binary, not copied")

cap = gm.INPUT_MAX_BYTES
p = mkfile(fdir, "exact.txt", "a" * cap)
row, tmp, se = run(files=[p], site="gemini")
i0 = (row.get("inputs") or [{}])[0]
check(i0.get("archived") and len(read_bytes(tmp, i0["archived"])) == cap, "a file of exactly the cap (1,000,000 bytes): copied whole")
p = mkfile(fdir, "big.txt", "a" * (cap + 1))
row, tmp, se = run(files=[p], site="gemini")
i0 = (row.get("inputs") or [{}])[0]
check(i0.get("why") == "too large" and "archived" not in i0 and i0.get("bytes") == cap + 1
      and i0.get("sha256") == sha(b"a" * (cap + 1)), "one byte over: recorded (size, sha256), never truncated, not copied")
row, tmp, se = run(stdin="b" * (cap + 1))
i0 = (row.get("inputs") or [{}])[0]
check(i0.get("why") == "too large" and "archived" not in i0 and i0.get("sha256") == sha(b"b" * (cap + 1)),
      "stdin over the cap: same, and no truncated stdin.txt exists")
check(not any(x.endswith("stdin.txt") for x in outputs_listing(tmp)), "...(listing confirms)")

files10 = [mkfile(fdir, f"many{n}.md", f"n{n}\n") for n in range(10)]
row, tmp, se = run(stdin="s\n", files=files10, site="gemini")
inp = row.get("inputs") or []
kept = [i for i in inp if i.get("archived")]
check(len(inp) == 11 and len(kept) == gm.INPUT_MAX_ITEMS and all(i.get("why") == "limit" for i in inp if not i.get("archived")),
      "11 inputs: the first 8 kept, the rest listed with why=limit (every one still has a sha256)")
check(all(i.get("sha256") for i in inp), "...every input has a sha256")

# unreadable: the run's own check decides what to do; this must just record it
gm._note_inputs("", ["/nonexistent/dir/gone.md"])
check(len(gm._INPUTS) == 1 and gm._INPUTS[0]["why"] == "unreadable" and gm._INPUTS[0]["bytes"] is None,
      "a file that cannot be read: noted as unreadable, no exception")

# ---- opt-outs --------------------------------------------------------------------------------------
row, tmp, se = run(stdin=DRAFT, files=[p1], site="gemini", env={"SMITH_NO_ARCHIVE": "1"})
check("inputs" not in row and "output_file" not in row and not os.path.exists(os.path.join(tmp, "outputs")),
      "SMITH_NO_ARCHIVE=1: no output, no inputs, no folder at all")
check(gm._INPUTS == [], "...and nothing was even read (the side channel is empty)")
row, tmp, se = run(stdin=DRAFT, files=[p1], site="gemini", env={"SMITH_ARCHIVE_INPUTS": "0"})
check("inputs" not in row and row.get("output_file") and not any(x.endswith(".in") for x in outputs_listing(tmp)),
      "SMITH_ARCHIVE_INPUTS=0: the OUTPUT is still archived, the inputs are not")
row, tmp, se = run(stdin=DRAFT, env={"SMITH_ARCHIVE_INPUTS": "1"})
check(row.get("inputs"), "SMITH_ARCHIVE_INPUTS=1 is not an off switch (only 0 is)")

# ---- only a reviewable run gets inputs ---------------------------------------------------------------
t = tempfile.mkdtemp(prefix="ia-")
gm._note_inputs("some draft", [])
rows = ledger_call(t, {"script": "gemini", "model": "m", "tag": "smoke"}, output=None)
check("inputs" not in rows[-1] and not os.path.exists(os.path.join(t, "outputs")), "a run with no output: no inputs (nothing to pair them with)")
rows = ledger_call(t, {"script": "witness", "model": "m"}, output="witness text")
check("inputs" not in rows[-1], "a witness row: no inputs")
rows = ledger_call(t, {"script": "gemini-raw", "model": "m"}, output="raw text")
check("inputs" not in rows[-1], "a row of another script: no inputs")

# ---- same-second runs never share or overwrite inputs ----------------------------------------------------
t = tempfile.mkdtemp(prefix="ia-")
TS = "2026-11-07T20:00:00"
gm._note_inputs("first draft\n", [])
r1 = ledger_call(t, {"ts": TS, "script": "gemini", "model": "m", "tag": "x"}, output="out one")[-1]
gm._note_inputs("second draft\n", [])
r2 = ledger_call(t, {"ts": TS, "script": "gemini", "model": "m", "tag": "x"}, output="out two")[-1]
check(r1["output_file"] != r2["output_file"] and r2["output_file"].endswith("-2.txt"), "two runs in one second: outputs get -2")
check(read_bytes(t, r1["inputs"][0]["archived"]) == b"first draft\n" and read_bytes(t, r2["inputs"][0]["archived"]) == b"second draft\n",
      "...and each keeps ITS OWN draft (the second did not overwrite the first)")
check(r1["inputs"][0]["archived"] != r2["inputs"][0]["archived"], "...in separate folders")

# ---- names cannot escape the folder -----------------------------------------------------------------------
t = tempfile.mkdtemp(prefix="ia-")
evil = mkfile(fdir, "sub/my draft (v2) ; rm -rf.md", "hello\n")
gm._note_inputs("", [evil, os.path.join(fdir, "sub", "..", "sub", "..", "a.md")])
r = ledger_call(t, {"script": "gemini", "model": "m", "tag": "x"}, output="o")[-1]
paths = [i["archived"] for i in r["inputs"]]
inside = all(os.path.realpath(os.path.join(t, p)).startswith(os.path.realpath(os.path.join(t, "outputs")) + os.sep) for p in paths)
check(inside and all(os.path.basename(p).replace("-", "").replace("_", "").replace(".", "").isalnum() for p in paths),
      "odd characters in a file name: the copy stays inside outputs/ with a safe name")
check(r["inputs"][0]["name"] == "my draft (v2) ; rm -rf.md", "...while the row keeps the real name")

# ---- state does not leak between runs --------------------------------------------------------------------------
gm._note_inputs("x", [])
gm._note_inputs("y", [], record=False)
check(gm._INPUTS == [], "record=False (--batch / --cascade) clears and keeps nothing")
row, tmp, se = run(stdin=DRAFT)
row2, tmp2, se2 = run()
check(row.get("inputs") and "inputs" not in row2, "a run after one that had stdin does not inherit its inputs")

# ---- --json trim and the raw copy ------------------------------------------------------------------------------
OBJ = '{"verdict":"fail","reason":"x"}'
row, tmp, se = run(stdin=DRAFT, canned=OBJ + ' junk fragment"}', flags=["--json"])
ins = [x for x in outputs_listing(tmp) if x.endswith(".in")]
check(row.get("output_file_raw") and len(ins) == 1 and ins[0] == row["output_file"][:-4] + ".in",
      "--json trim writes a raw copy of the OUTPUT too, but only one .in folder, named for the delivered output")

# ---- lifecycle: the record is checked against the artifact it describes -----------------------------------------
bad = []
for r, t in ((row, tmp),):
    for i in r["inputs"]:
        if i.get("archived") and sha(read_bytes(t, i["archived"])) != i["sha256"]:
            bad.append(i["archived"])
check(not bad, "every recorded sha256 equals the hash of the file now on disk")

# a copy that is edited afterwards no longer matches (the record is a hash, not a promise)
ap = os.path.join(tmp, row["inputs"][0]["archived"])
with open(ap, "a") as f:
    f.write("edited later")
check(sha(read_bytes(tmp, row["inputs"][0]["archived"])) != row["inputs"][0]["sha256"],
      "...and an edited copy is detectable: the recorded hash no longer matches it")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
