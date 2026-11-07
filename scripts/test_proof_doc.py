#!/usr/bin/env python3
"""Probes for proof_doc.py, which turns "CSS byte identical, 147/147 numbers" from a free-text note
into a file you can attach.

Each case damages one thing in an otherwise good page and asserts that exactly the right check
fails, with the evidence in its detail. The shell here carries the same trap the real one does: its
header comment contains the literal text "<style>", which sent a regex-based extraction into the
comment and made a clean page look modified (found 2026-11-07).

Pure stdlib.   python3 -B scripts/test_proof_doc.py
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("proof_doc", os.path.join(_HERE, "proof_doc.py"))
pd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pd)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


CSS = "\nbody{font:15px system-ui}.wrap{max-width:860px}\nh1{font-size:25px}\n"
SHELL = f"""<!DOCTYPE html>
<!-- house shell v1. Fleet rule: reuse this <style> block VERBATIM, change nothing else. -->
<html lang="en">
<head>
<meta charset="utf-8">
<title>TITLE HERE</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<!-- CONTENT HERE: h1 + .sub header, then h2 sections -->
</div>
</body>
</html>
"""

SRC = """# Quarterly Plan

Revenue was $1,200 in 2026, up 3.5% on the prior year. TarotCore ships in 147 days.

## Goals

- Ship the beta
- Reduce churn to 2.1%

1. Interview ten users

| Name | Value |
|------|-------|
| Alpha | $99 |
| Beta | 4,500 |

See [the docs](https://example.com/docs) and `make test` for details.

> Note: R&D is excluded.
"""

BODY = """<h1>Quarterly Plan</h1>
<p>Revenue was $1,200 in 2026, up 3.5% on the prior year. TarotCore ships in 147 days.</p>
<h2>Goals</h2>
<ul><li>Ship the beta</li><li>Reduce churn to 2.1%</li></ul>
<ol><li>Interview ten users</li></ol>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td class="num">$99</td></tr><tr><td>Beta</td><td class="num">4,500</td></tr></table>
<p>See <a href="https://example.com/docs">the docs</a> and <code>make test</code> for details.</p>
<p>Note: R&amp;D is excluded.</p>"""


def page(body=BODY, title="Quarterly Plan", css=CSS, shell=SHELL):
    p = shell.replace("TITLE HERE", title).replace("<!-- CONTENT HERE: h1 + .sub header, then h2 sections -->", body)
    return p.replace(f"<style>{CSS}</style>", f"<style>{css}</style>")


def run(p, src=SRC):
    return {n: (st, d) for st, n, d in pd.run_checks(p, SHELL, src)}


def failed(res):
    return sorted(n for n, (st, _) in res.items() if st == "FAIL")


# ---- a good page ----------------------------------------------------------------------------------
res = run(page())
check(failed(res) == [], "a faithful page: no check fails" + ("" if failed(res) == [] else f"  {failed(res)}: {[res[n][1] for n in failed(res)]}"))
check(res["shell CSS"][0] == "PASS" and f"({len(CSS)} chars)" in res["shell CSS"][1],
      "...the CSS is read past the shell's header comment that contains '<style>' (the regex trap)")
check(pd.parse(SHELL).styles == [CSS], "...parse(): the shell has exactly one style block and it excludes the comment")
check(res["figures"][0] == "PASS" and "all 7 figures" in res["figures"][1], "...7 figures: $1,200 2026 3.5% 147 2.1% $99 4,500 (single digits are not figures)")
check(res["words"][0] == "PASS" and res["headings"][0] == "PASS", "...markdown syntax (bold, lists, table rules, link URL, code, quote) causes no false failure")
no_src = run(page(), None)
check(failed(no_src) == [] and no_src["content parity"][0] == "INFO" and "NOT CHECKED" in no_src["content parity"][1],
      "without a draft: structure passes and content parity is stated as NOT CHECKED")

# ---- the shell CSS ---------------------------------------------------------------------------------------
res = run(page(css=CSS.replace("860px", "861px")))
check(failed(res) == ["shell CSS"] and "'860px'" not in res["shell CSS"][1] and "first difference" in res["shell CSS"][1],
      "one changed character in the CSS: only 'shell CSS' fails, and it points at the difference")
res = run(page(css=CSS.replace("h1{font-size:25px}\n", "")))
check(failed(res) == ["shell CSS"], "a trimmed CSS (the incident: 2326 -> 2040 chars, still rendered): fails")
res = run(page(css=CSS + "\n"))
check(failed(res) == ["shell CSS"], "one extra trailing newline in the block: fails (byte-identical means byte-identical)")
res = run(page().replace("</head>", "<style>.extra{color:red}</style>\n</head>"))
check(failed(res) == ["shell CSS"] and "extra CSS" in res["shell CSS"][1], "a second <style> block: fails, 'extra CSS was added'")
res = run(page().replace(f"<style>{CSS}</style>", ""))
check(failed(res) == ["shell CSS"] and "no <style>" in res["shell CSS"][1], "no <style> block at all: fails")

# ---- title and placeholders -------------------------------------------------------------------------------
res = run(page(title="TITLE HERE"))
check(failed(res) == ["placeholders", "title"], "the shell's 'TITLE HERE' left as the title: title and placeholders fail")
res = run(page(title="   "))
check(failed(res) == ["title"], "an empty title: fails")
res = run(page(body=BODY + "\n<p>TITLE HERE</p>"), SRC + "\nTITLE HERE\n")
check("placeholders" in failed(res), "'TITLE HERE' anywhere in the body: fails")

# ---- complete and well-formed -----------------------------------------------------------------------------------
whole = page()
res = run(whole[:whole.index("</body>")])
check(failed(res) == ["complete"] and "never reaches </html>" in res["complete"][1], "truncated before </html>: 'complete' fails")
cut = page()[:page().index("<h2>Goals</h2>")]
res = run(cut)
check("complete" in failed(res) and "<div> was never closed" in res["complete"][1],
      "a page cut off mid-content: the report names the element left open (<div>), not just 'truncated'")
res = run(page().replace("</div>", "", 1))
check(failed(res) == ["complete"] and "<div> was never closed" in res["complete"][1], "an unclosed <div>: fails")
res = run(page().replace("</h1>", "</h1></span>", 1))
check(failed(res) == ["complete"] and "stray </span>" in res["complete"][1], "a stray closing tag: fails")
res = run(page().replace("</body>", "</body><body></body>"))
check("complete" in failed(res), "two <body> elements: fails")
res = run(page(body=BODY.replace("<li>Ship the beta</li>", "<li>Ship the beta").replace("<li>Reduce", "<li>Reduce")))
check("complete" not in failed(res), "omitted </li> is legal HTML: not a failure")

# ---- nothing outside the document -----------------------------------------------------------------------------------
res = run("```html\n" + page() + "\n```")
check(failed(res) == ["outside"] and "code fence" in res["outside"][1] and "before the document" in res["outside"][1] and "after </html>" in res["outside"][1],
      "wrapped in a code fence (the real 2026-09-24 answer): 'outside' fails, names both fences")
res = run(page() + "\nHope that helps!")
check(failed(res) == ["added text", "outside"], "prose after </html>: 'outside' fails, and it is also 3 added words, so 'added text' fails too")
res = run("Here is the HTML:\n" + page())
check(failed(res) == ["added text", "outside"], "prose before the document: 'outside' fails, and 'added text' with it")
check(failed(run(page() + "\nHope that helps!", None)) == ["outside"], "...with no draft to compare, only 'outside' can fail")
res = run("﻿" + page() + "\n\n")
check(failed(res) == [], "a BOM and trailing blank lines are not 'outside text'")

# ---- self-contained -----------------------------------------------------------------------------------------------------
for what, snippet in (("a stylesheet <link>", '<link rel="stylesheet" href="https://cdn.example/x.css">'),
                      ("a <script src>", '<script src="//cdn.example/x.js"></script>'),
                      ("an <img src>", '<img src="https://cdn.example/x.png">'),
                      ("an <iframe>", '<iframe src="http://example.com/"></iframe>')):
    res = run(page().replace("</head>", snippet + "\n</head>") if "link" in what or "script" in what else page(body=BODY + snippet))
    check("self-contained" in failed(res), f"{what}: fails")
res = run(page().replace("</head>", "<style>@import url(https://fonts.example/x.css);</style>\n</head>"))
check("self-contained" in failed(res), "a CSS @import: fails")
res = run(page(body=BODY + '<img src="data:image/png;base64,AAAA" alt="">'), SRC)
check("self-contained" not in failed(res), "a data: URI image is self-contained")
check(run(page())["self-contained"][0] == "PASS", "an ordinary <a href='https://...'> link is content, not an asset")

# ---- elision ------------------------------------------------------------------------------------------------------------------
for what, snippet in (("[...]", "<p>[...]</p>"), ("a remaining-sections note", "<p>(remaining sections omitted)</p>"),
                      ("an HTML comment", "<!-- rest unchanged -->")):
    res = run(page(body=BODY + snippet), SRC)
    check("elision" in failed(res), f"elision marker, {what}: fails")

# ---- content parity: figures --------------------------------------------------------------------------------------------------
res = run(page(body=BODY.replace("$1,200", "$1200")))
check("figures" in failed(res) and "$1,200" in res["figures"][1], "'$1,200' reformatted as '$1200': figures fails and names it")
res = run(page(body=BODY.replace("3.5%", "3.6%")))
check("figures" in failed(res) and "3.5%" in res["figures"][1], "a changed figure (3.5% -> 3.6%): fails")
res = run(page(body=BODY.replace("<td>Beta</td><td class=\"num\">4,500</td>", "")))
check("figures" in failed(res) and "words" in failed(res), "a dropped table row: figures and words both fail")

# ---- content parity: words ------------------------------------------------------------------------------------------------------
res = run(page(body=BODY.replace(" ships in 147 days", "")))
check("words" in failed(res) and "shipped"[:0] == "" and "ships" in res["words"][1], "a dropped clause: words fails and shows what is missing")
res = run(page(body=BODY.replace("TarotCore", "TatartCore")))
check("words" in failed(res) and "tarotcore" in res["words"][1], "a changed proper noun (TarotCore -> TatartCore, the real incident): words fails and names it")
res = run(page(body=BODY.replace("<li>Ship the beta</li>", "")))
check("words" in failed(res), "a dropped list item: fails")
res = run(page(body=BODY + "<p>This quarter we also quietly doubled the budget.</p>"))
check(failed(res) == ["added text"] and "doubled the budget" in res["added text"][1], "an invented sentence: 'added text' fails and quotes it")
res = run(page(body=BODY.replace("<h2>Goals</h2>", "<h2>Goals (draft)</h2>")), SRC)
check("added text" not in failed(res) and "words" not in failed(res), "one or two added words (a label) are tolerated")
res = run(page(body=BODY.replace("Quarterly Plan", "Quarterly Plan", 1)))
check(res["words"][0] == "PASS", "identical text: words passes")

# ---- headings ------------------------------------------------------------------------------------------------------------------------
res = run(page(body=BODY.replace("<h2>Goals</h2>", "<p>Goals</p>")))
check("headings" in failed(res) and "Goals".lower() in res["headings"][1], "a heading demoted to a paragraph: headings fails")
two = SRC + "\n## Risks\n\nNone known.\n"
twob = BODY + "\n<h2>Risks</h2>\n<p>None known.</p>"
check(run(page(body=twob), two)["headings"][0] == "PASS", "two draft headings in order: passes")
swapped = BODY.replace("<h2>Goals</h2>", "<h2>Risks</h2><p>None known.</p><h2>Goals</h2>")
res = run(page(body=swapped + ""), two)
check("headings" in failed(res), "headings in the wrong order: fails")

# ---- the CLI ----------------------------------------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="pd-")
shell_f, src_f, good_f = (os.path.join(tmp, n) for n in ("shell.html", "draft.md", "good.html"))
open(shell_f, "w").write(SHELL)
open(src_f, "w").write(SRC)
open(good_f, "w").write(page())
bad_f = os.path.join(tmp, "bad.html")
open(bad_f, "w").write(page(title="TITLE HERE"))


def cli(*args):
    p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "proof_doc.py"), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


rc, out, _ = cli(good_f, "--source", src_f, "--shell", shell_f)
check(rc == 0 and "failed 0" in out and "content parity: CHECKED" in out, "CLI: faithful page + draft: exit 0")
rc, out, _ = cli(good_f, "--shell", shell_f)
check(rc == 3 and "NOT CHECKED" in out, "CLI: faithful page, no draft: exit 3 (a partial proof says so)")
rc, out, _ = cli(bad_f, "--source", src_f, "--shell", shell_f)
check(rc == 1 and "FAIL title" in out, "CLI: a failing page: exit 1")
rc, out, err = cli(os.path.join(tmp, "nope.html"), "--shell", shell_f)
check(rc == 2 and out == "" and "cannot read" in err, "CLI: an unreadable answer: exit 2, nothing on stdout")
rc, out, err = cli(good_f, "--source", os.path.join(tmp, "nope.md"), "--shell", shell_f)
check(rc == 2 and out == "", "CLI: an unreadable draft: exit 2, nothing on stdout")
rc, out, _ = cli(good_f, "--source", src_f, "--shell", shell_f)
import hashlib
check(hashlib.sha256(open(good_f, "rb").read()).hexdigest() in out and hashlib.sha256(open(src_f, "rb").read()).hexdigest() in out
      and hashlib.sha256(open(shell_f, "rb").read()).hexdigest() in out,
      "the report names the sha256 of the answer, the draft AND the shell it compared against")
check("does NOT" in out and "checked at" in out, "...and the time, and what it does not prove")

# ---- the proof attaches -----------------------------------------------------------------------------------------------------------------------
led = os.path.join(tmp, "usage.jsonl")
open(led, "w").write('{"ts":"2026-11-01T10:00:01","run_id":"aaaaaaaaaaaa","script":"gemini","model":"m","tag":"doc-format"}\n')
proof_f = os.path.join(tmp, "proof.txt")
open(proof_f, "w").write(out)
v = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "verdict.py"), "good", "ok", "--run-id", "aaaaaaaaaaaa", "--tag", "doc-format", "--evidence", proof_f],
                   env=dict(os.environ, SMITH_LEDGER=led, SMITH_VERDICT_DB=os.path.join(tmp, "v.db")), capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(v.returncode == 0 and "evidence/" in v.stdout, "the report is accepted by `verdict.py --evidence`")

# ---- the real shell: a page made from it passes -------------------------------------------------------------------------------------------
real = os.path.join(os.path.dirname(_HERE), "references", "html-doc-shell.html")
if os.path.isfile(real):
    rs = open(real, encoding="utf-8").read()
    made = rs.replace("TITLE HERE", "Quarterly Plan").replace(
        [l for l in rs.splitlines() if "CONTENT HERE" in l or l.startswith("<!-- CONTENT")][0], "<h1>Quarterly Plan</h1>")
    res = {n: st for st, n, d in pd.run_checks(made, rs, None)}
    check(res["shell CSS"] == "PASS" and res["complete"] == "PASS", "the REAL house shell, filled in, passes shell CSS and complete (the regex trap is real there too)")

print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
