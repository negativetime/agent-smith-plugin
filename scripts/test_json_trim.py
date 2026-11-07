#!/usr/bin/env python3
"""Probes for gemini.py --json: keep the first complete JSON value, and say what happened.

Measured 2026-11-07 on 7,994 archived JSON-looking answers. classify @ glm-5.3 returned 116 of
2,755 that did not parse, and the first reading of that number was wrong:
  81  an intentional row format  `[0,"ADS","H"],[1,"PERSONAL","H"],...`  (valid once wrapped in [ ]);
      "keep the first value" would have kept ONE row and deleted the other 89
  10  a complete object followed by a corrupted fragment of another answer (trimmable)
  23  unescaped quotes inside a string                                    (left alone)
   2  truncated                                                           (left alone)
Nothing logged any of it. So this trims ONLY when no other JSON value follows the first complete
object/array, leaves everything else byte for byte, and records json_status in the ledger.

Pure stdlib: the backend call is stubbed; no model, no network.   python3 -B scripts/test_json_trim.py
"""
import contextlib
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


# Shapes taken from the measured failures, with invented content.
OBJ = '{"verdict":"fail","reason":"The step invents a task and platform that the note never mentions."}'
JUNK = ' \'Chad B.\' and \'real sample\', fits 5 minutes, and invents no specifics."}'
ROWS = '[0,"UNKNOWN","H"],[1,"PERSONAL","H"],[2,"ADS","M"],[3,"UNKNOWN","H"]'
SECOND_OBJECT = OBJ + "\n" + '{"verdict":"pass","reason":"A second, real answer."}'


def pj(text, want=True, trim=True):
    return gm._json_pass(text, want, trim)


# ---- unit: every status ------------------------------------------------------------------------
out, info = pj(OBJ)
check(out == OBJ and info["json_status"] == "ok" and info["json_flag"] is True, "a whole valid object: ok, unchanged")

out, info = pj(OBJ + JUNK)
check(out == OBJ and info["json_status"] == "trimmed", "a complete object + a corrupted fragment: trimmed to the object")
check(info["json_dropped_chars"] == len(JUNK.strip()) and info["json_dropped"] == JUNK.strip(),
      "...the ledger fields say how much was dropped and show it")
check(json.loads(out)["verdict"] == "fail", "...and what is delivered parses")

out, info = pj(OBJ + JUNK, want=False)
check(out == OBJ + JUNK and info["json_status"] == "trimmable" and info["json_flag"] is False,
      "the same answer WITHOUT --json: delivered unchanged, status trimmable, json_flag False")

out, info = pj(OBJ + JUNK, want=True, trim=False)
check(out == OBJ + JUNK and info["json_status"] == "trimmable", "trim switched off: delivered unchanged, status trimmable")

out, info = pj(OBJ + "}")
check(out == OBJ and info["json_status"] == "trimmed", "a stray closing brace after the object is dropped")

# the case that nearly bit: rows in a row
out, info = pj(ROWS)
check(out == ROWS and info["json_status"] == "multiple",
      "ROW SEQUENCE `[..],[..],[..]` is never trimmed (a trim would have kept one row and deleted the rest)")
check(json.loads("[" + out + "]")[2] == [2, "ADS", "M"], "...and it is intact: valid once the caller wraps it in [ ]")

out, info = pj(SECOND_OBJECT)
check(out == SECOND_OBJECT and info["json_status"] == "multiple", "an object followed by a second valid object: multiple, unchanged")

out, info = pj(OBJ + " see [1, 2] for details")
check(out == OBJ + " see [1, 2] for details" and info["json_status"] == "multiple",
      "a tail containing even a small valid array is left alone (a doubt never deletes data)")

out, info = pj(OBJ + " " + "[" * 300)
check(info["json_status"] == "multiple", "more than 200 bracket candidates in the tail: assumed to hold a value, left alone")

out, info = pj('{"verdict":"fail","reason":"cut off in the mid')
check(out == '{"verdict":"fail","reason":"cut off in the mid' and info["json_status"] == "malformed",
      "truncated: malformed, delivered unchanged (never completed)")

q = '{"verdict":"fail","reason":"The note says "figure out key" and nothing else."}'
out, info = pj(q)
check(out == q and info["json_status"] == "malformed", "unescaped quotes inside a string: malformed, unchanged")

out, info = pj('Here is the result: ' + OBJ)
check(out == 'Here is the result: ' + OBJ and info["json_status"] == "malformed",
      "JSON after a line of prose: never fished out; malformed, unchanged")

fenced = "```json\n" + OBJ + "\n```"
out, info = pj(fenced)
check(out == fenced and info["json_status"] == "fenced", "valid JSON inside a code fence: fenced, delivered UNCHANGED (callers match fences)")
out, info = pj(fenced, want=False)
check(out == fenced and info["json_status"] == "fenced", "...also without --json")

out, info = pj("")
check(out == "" and info["json_status"] == "malformed", "empty answer with --json: malformed")
check(pj("42")[1]["json_status"] == "ok", "a bare scalar with --json: ok")

# runs that did not ask for JSON and do not look like it get NOTHING
for what, text in (("plain prose", "The answer is that it depends."), ("a number", "42"),
                   ("fenced html", "```html\n<p>hi</p>\n```"), ("code", "def f():\n    return 1\n")):
    out, info = pj(text, want=False)
    check(out == text and info == {}, f"no --json and not JSON-looking ({what}): no fields, unchanged")

# ---- the guard never alters anything it does not trim ----------------------------------------------
samples = [OBJ, OBJ + JUNK, ROWS, SECOND_OBJECT, fenced, "prose", "", '{"a":', "[1,2,3]", '{"a":1}}']
check(all(pj(s, want=False)[0] == s for s in samples), "without --json, nothing is ever altered")
check(all(pj(s)[0] == s for s in samples if pj(s)[1].get("json_status") != "trimmed"),
      "with --json, only a 'trimmed' answer is ever altered")


# ---- end to end through main(), all three output sites ----------------------------------------------
def run(canned, flags=(), env=None, site="openai"):
    tmp = tempfile.mkdtemp(prefix="jt-")
    saved = {k: os.environ.get(k) for k in ("SMITH_LEDGER", "SMITH_NO_ARCHIVE", "SMITH_JSON_TRIM",
                                            "GEMINI_API_KEY", "ZAI_API_KEY")}
    os.environ["SMITH_LEDGER"] = os.path.join(tmp, "usage.jsonl")
    os.environ.pop("SMITH_NO_ARCHIVE", None)
    os.environ.pop("SMITH_JSON_TRIM", None)
    os.environ.update(env or {})
    old = (gm.call_openai_compat, gm.http_json, sys.argv, sys.stdin)
    argv = ["gemini.py", "--tag", "smoke", *flags, "grade this"]
    if site == "openai":
        gm.call_openai_compat = lambda *a, **k: canned
        argv = ["gemini.py", "--backend", "openai", "--model", "stub-model", "--tag", "smoke", *flags, "grade this"]
    elif site == "gemini":
        os.environ["GEMINI_API_KEY"] = "k"
        gm.http_json = lambda *a, **k: {"candidates": [{"content": {"parts": [{"text": canned}]},
                                                        "finishReason": "STOP"}], "usageMetadata": {}}
    elif site == "quota":
        os.environ["GEMINI_API_KEY"], os.environ["ZAI_API_KEY"] = "k", "z"

        def boom(*a, **k):
            raise gm.QuotaExhausted("cap")
        gm.http_json = boom
        gm.call_openai_compat = lambda *a, **k: canned
        argv = ["gemini.py", "--search", "--tag", "smoke", *flags, "grade this"]
    sys.argv, sys.stdin = argv, io.StringIO("")
    so, se = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            try:
                gm.main()
            except SystemExit:
                pass
    finally:
        gm.call_openai_compat, gm.http_json, sys.argv, sys.stdin = old
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rows = [json.loads(l) for l in open(os.path.join(tmp, "usage.jsonl")) if l.strip()]
    row = [r for r in rows if r.get("script") == "gemini"][-1]
    read = lambda rel: open(os.path.join(tmp, rel)).read() if rel else None
    return so.getvalue(), se.getvalue(), row, read(row.get("output_file")), read(row.get("output_file_raw")), tmp


so, se, row, arch, raw, _ = run(OBJ + JUNK, ["--json"])
check(so == OBJ + "\n", "main(): stdout is the first value alone")
check(row["json_status"] == "trimmed" and row["json_flag"] is True and row["json_dropped_chars"] == len(JUNK.strip()),
      "...the ledger row carries json_status, json_flag and json_dropped_chars")
check(arch == OBJ and raw == OBJ + JUNK, "...output_file holds what was delivered and output_file_raw holds the model's raw answer")
check(row["out_chars"] == len(OBJ), "...out_chars is the delivered length")
check("[json] kept the first complete JSON value" in se, "...and stderr says so")
check(row["json_dropped"] == JUNK.strip(), "...the dropped text is previewed in the row")

so, se, row, arch, raw, _ = run(OBJ + JUNK)
check(so == OBJ + JUNK + "\n" and row["json_status"] == "trimmable" and row["json_flag"] is False and raw is None,
      "main() without --json: unchanged, status trimmable, no raw copy")

so, se, row, arch, raw, _ = run(ROWS, ["--json"])
check(so == ROWS + "\n" and row["json_status"] == "multiple", "main(): a row sequence with --json comes out byte for byte")

so, se, row, arch, raw, _ = run(OBJ, ["--json"])
check(row["json_status"] == "ok" and "output_file_raw" not in row and "json_dropped" not in row,
      "main(): a clean answer logs ok with no raw copy and no dropped fields")

so, se, row, arch, raw, _ = run("Just prose, no JSON asked.")
check(not any(k.startswith("json") for k in row), "main(): an unrelated prose answer gets no json fields at all")

so, se, row, arch, raw, _ = run(OBJ + JUNK, ["--json"], env={"SMITH_JSON_TRIM": "0"})
check(so == OBJ + JUNK + "\n" and row["json_status"] == "trimmable", "main(): SMITH_JSON_TRIM=0 delivers unchanged")

so, se, row, arch, raw, _ = run('{"a":', ["--json"])
check("WARNING" in se and row["json_status"] == "malformed", "main(): --json with a truncated answer warns on stderr")

so, se, row, arch, raw, _ = run(OBJ + JUNK, ["--schema", os.devnull])
check(so == OBJ + "\n", "main(): --schema counts as asking for JSON")

so, se, row, arch, raw, _ = run(OBJ + " " + "z" * 5000, ["--json"])
check(row["json_status"] == "trimmed" and len(row["json_dropped"]) == gm.JSON_DROP_PREVIEW and row["json_dropped_chars"] == 5000,
      "main(): a 5,000-character tail is previewed at 160 characters, so the ledger stays one short line")

so, se, row, arch, raw, _ = run(OBJ + JUNK, ["--json"], site="gemini")
check(so == OBJ + "\n" and row["json_status"] == "trimmed" and raw == OBJ + JUNK, "main(): the Gemini API path is wired the same way")

so, se, row, arch, raw, _ = run(OBJ + JUNK, ["--json"], site="quota")
check(so == OBJ + "\n" and row["json_status"] == "trimmed" and row.get("fallback_from"), "main(): the quota-fallback path is wired the same way")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
