#!/usr/bin/env python3
"""Proof that an HTML-ified doc is faithful to the house shell and (given the draft) to its content.

    python3 proof_doc.py doc.html --source draft.md > proof.txt
    python3 verdict.py good "<note>" --tag doc-format --evidence proof.txt

The doc-format route converts a markdown draft into HTML with the house shell
(references/html-doc-shell.html): "reuse the <style> block VERBATIM, preserve ALL content, change
nothing else". Every `good` on that route claims it, and until now the claim lived in a free-text
note ("CSS byte identical", "147/147 numbers"). This turns it into a file you can attach.

Always checked (no draft needed):
  shell CSS     the document's <style> block is byte-identical to the shell's, and is the only one
  title         <title> is present and is not the shell's "TITLE HERE" placeholder
  placeholders  no "TITLE HERE" anywhere
  complete      starts at <!DOCTYPE>/<html>, ends at </html>, tags balance, one html/head/body
  outside       no text before the document or after </html> (a stray ``` code fence is the usual one)
  self-contained no external stylesheet/script/image/font/@import/url(); an ordinary <a href> link is fine
  elision       no "[...]", "(remaining sections omitted)" and the like
Checked only with --source DRAFT.md (this is "content parity"):
  figures       every number in the draft is in the page with the SAME formatting ($99, 1,200, 3.5%)
  words         the draft's words appear in the page, in order. Any missing word fails (a dropped
                sentence, a changed name like TarotCore -> TatartCore). Added text of 3+ words fails
  headings      every draft heading is a page heading, in order

Exit code: 0 every check passed AND the draft was given; 1 any failed; 3 nothing failed but content
parity was NOT checked (no --source), which is a partial proof and says so; 2 unreadable input.

Why a real HTML parser: the shell's own header comment contains the literal text "<style>", so a
regex starts the "CSS" inside the comment and reports a clean page as modified. Found 2026-11-07.
It proves the page matches the draft you give it; it does not prove the draft was right. Python
stdlib only.
"""
import argparse
import datetime
import difflib
import hashlib
import html
import os
import re
import sys
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SHELL = os.path.join(os.path.dirname(HERE), "references", "html-doc-shell.html")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
OPTIONAL_END = {"p", "li", "td", "th", "tr", "thead", "tbody", "tfoot", "colgroup", "dt", "dd", "option",
                "html", "head", "body"}
SKIP_TEXT = {"style", "script", "title", "head", "template"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
ASSET_ATTRS = {"link": "href", "script": "src", "img": "src", "iframe": "src", "source": "src",
               "video": "src", "audio": "src", "embed": "src", "object": "data", "input": "src"}
ELISION = re.compile(r"\[\s*\.\.\.\s*\]|\[\s*…\s*\]|\((?:the )?(?:remaining|rest of|other)[^)]{0,60}"
                     r"(?:omitted|unchanged|same|truncated)[^)]*\)|\[(?:truncated|omitted)\]|"
                     r"<!--\s*(?:rest|remaining|same as|unchanged|omitted|continue)", re.I)
FIGURE = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")
TOKEN = re.compile(r"[^\W_]+")


class Doc(HTMLParser):
    """One pass over the page: styles, title, visible text, structure, external references."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.styles, self.title, self.stack, self.problems = [], "", [], []
        self.text, self.headings, self.external = [], [], []
        self.counts = {"html": 0, "head": 0, "body": 0, "table": 0, "tr": 0, "li": 0}
        self._skip, self._heading = 0, None

    def handle_starttag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1
        a = dict(attrs)
        if tag in ASSET_ATTRS:
            ref = (a.get(ASSET_ATTRS[tag]) or "").strip()
            if re.match(r"(?i)(https?:)?//", ref):
                self.external.append(f"<{tag} {ASSET_ATTRS[tag]}> {ref[:80]}")
        if tag == "style":
            self.styles.append("")
        if tag in SKIP_TEXT:
            self._skip += 1
        if tag in HEADINGS:
            self._heading = []
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in SKIP_TEXT:
            self._skip = max(0, self._skip - 1)
        if tag in HEADINGS and self._heading is not None:
            self.headings.append(" ".join(self._heading).split())
            self._heading = None
        if tag in VOID:
            return
        if tag not in self.stack:
            self.problems.append(f"stray </{tag}>")
            return
        while self.stack[-1] != tag:
            t = self.stack.pop()
            if t not in OPTIONAL_END:
                self.problems.append(f"<{t}> was never closed")
        self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "style":
            self.styles[-1] += data
        elif self.stack and self.stack[-1] == "title":
            self.title += data
        elif not self._skip:
            self.text.append(data)
            if self._heading is not None:
                self._heading.append(data)


def parse(text):
    d = Doc()
    d.feed(text)
    d.close()
    d.problems += [f"<{t}> was never closed" for t in d.stack if t not in OPTIONAL_END]
    return d


def words(text):
    return TOKEN.findall(text.lower())


def md_to_text(md):
    """The draft as the page would show it: markdown syntax removed, link text kept, URLs of
    [text](url) dropped (they live in an href, not in the visible text)."""
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.S)
    md = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    out = []
    for line in md.splitlines():
        if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line) and "-" in line:      # table rule or hr
            continue
        if re.fullmatch(r"\s*(`{3,}|~{3,}).*", line) and len(line.strip()) <= 12:
            continue
        line = re.sub(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", "", line)
        out.append(line.replace("|", " ").replace("`", "").replace("*", ""))
    return "\n".join(out)


def md_headings(md):
    return [words(m.group(1)) for m in re.finditer(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", md, re.M)]


def figures(text):
    """Numbers worth checking by exact formatting: two or more digits, or a decimal, percent or
    currency sign. Single digits are skipped: list numbering and 'step 1' are not figures."""
    out = []
    for m in FIGURE.finditer(text):
        f = m.group(0).rstrip(",")
        digits = re.sub(r"\D", "", f)
        if (len(digits) >= 2 or "." in f or "%" in f or f[0] in "$€£") and f not in out:
            out.append(f)
    return out


def sha(b):
    return hashlib.sha256(b).hexdigest()


def run_checks(page, shell, source=None):
    """[(status, name, detail)] where status is PASS / FAIL / INFO. `source` None means parity is
    not checked."""
    out = []
    add = lambda st, name, detail: out.append((st, name, detail))
    doc, ref = parse(page), parse(shell)
    shell_css = ref.styles[0] if ref.styles else None

    if shell_css is None:
        add("FAIL", "shell CSS", "the shell has no <style> block; nothing to compare against")
    elif not doc.styles:
        add("FAIL", "shell CSS", "the page has no <style> block")
    elif doc.styles[0] != shell_css:
        sm = difflib.SequenceMatcher(None, shell_css, doc.styles[0], autojunk=False)
        op = next((o for o in sm.get_opcodes() if o[0] != "equal"), None)
        where = (f"; first difference at char {op[1]}: shell {shell_css[op[1]:op[2]][:40]!r} -> page "
                 f"{doc.styles[0][op[3]:op[4]][:40]!r}") if op else ""
        add("FAIL", "shell CSS", f"the <style> block is NOT byte-identical to the shell's "
                                 f"({len(doc.styles[0])} vs {len(shell_css)} chars){where}")
    elif len(doc.styles) > 1:
        add("FAIL", "shell CSS", f"the first <style> block matches the shell, but the page has {len(doc.styles)}: "
                                 f"extra CSS was added")
    else:
        add("PASS", "shell CSS", f"the <style> block is byte-identical to the shell's ({len(shell_css)} chars) and is the only one")

    t = doc.title.strip()
    if not t:
        add("FAIL", "title", "the page has no <title>, or it is empty")
    elif t.upper() == "TITLE HERE":
        add("FAIL", "title", "the <title> is still the shell's placeholder 'TITLE HERE'")
    else:
        add("PASS", "title", f"the <title> is set ({t[:60]!r})")

    add("FAIL" if "TITLE HERE" in page else "PASS", "placeholders",
        "'TITLE HERE' is still in the page" if "TITLE HERE" in page else "no 'TITLE HERE' left")

    s = page.lstrip("﻿")
    m = re.search(r"(?i)<!doctype|<html", s)
    low = s.lower()
    end = low.rfind("</html>")
    problems = list(doc.problems)
    if not m:
        problems.append("no <!DOCTYPE> or <html> found")
    if end < 0:
        problems.append("it never reaches </html> (truncated?)")
    for tag in ("html", "head", "body"):
        if doc.counts.get(tag, 0) != 1:
            problems.append(f"{doc.counts.get(tag, 0)} <{tag}> elements (expected 1)")
    add("FAIL" if problems else "PASS", "complete",
        "; ".join(problems[:5]) if problems else "starts at <!DOCTYPE>, ends at </html>, tags balance, one html/head/body")

    before = s[:m.start()].strip() if m else ""
    after = s[end + len("</html>"):].strip() if end >= 0 else ""
    if before or after:
        bits = ([f"before the document: {before[:40]!r}"] if before else []) + ([f"after </html>: {after[:40]!r}"] if after else [])
        add("FAIL", "outside", "text outside the document, " + "; ".join(bits) +
            ("  (a code fence around the HTML)" if (before or after).startswith("```") else ""))
    else:
        add("PASS", "outside", "nothing before the document or after </html>")

    css_ext = [f"CSS {x[:60]}" for blk in doc.styles for x in re.findall(r"(?i)@import[^;]*;|url\(\s*['\"]?(?:https?:)?//[^)]*\)", blk)]
    ext = doc.external + css_ext
    add("FAIL" if ext else "PASS", "self-contained",
        f"{len(ext)} external asset reference(s): " + "; ".join(ext[:3]) if ext else "no external stylesheet, script, image, font, @import or url()")

    visible = " ".join(doc.text)
    el = ELISION.search(page)
    add("FAIL" if el else "PASS", "elision", f"an elision marker: {el.group(0)[:60]!r}" if el else "no elision markers")

    if source is None:
        add("INFO", "content parity", "NOT CHECKED: no --source draft was given, so this proof says nothing about whether "
                                      "the page keeps the draft's content")
        return out

    src_text = md_to_text(source)
    figs = figures(source)
    missing = [f for f in figs if f not in visible]
    add("FAIL" if missing else "PASS", "figures",
        (f"{len(figs) - len(missing)} of {len(figs)} figures present; missing: " + ", ".join(missing[:8])) if missing
        else f"all {len(figs)} figures in the draft are in the page with the same formatting")

    sw, pw = words(src_text), words(visible)
    sm = difflib.SequenceMatcher(None, sw, pw, autojunk=False)
    lost, added = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            lost.append((i1, i2))
        if tag in ("insert", "replace") and j2 - j1 >= 3:
            added.append((j1, j2))
    kept = sum(b.size for b in sm.get_matching_blocks())
    if lost:
        shown = "; ".join(f"...{' '.join(sw[max(0, a - 2):a])} [[{' '.join(sw[a:b][:12])}]] ..." for a, b in lost[:3])
        add("FAIL", "words", f"{len(sw) - kept} of {len(sw)} draft words are missing or changed in the page: {shown}")
    else:
        add("PASS", "words", f"all {len(sw)} words of the draft are in the page, in order")
    if added:
        shown = "; ".join(f"[[{' '.join(pw[a:b][:12])}]]" for a, b in added[:3])
        add("FAIL", "added text", f"{len(added)} run(s) of 3+ words in the page that are not in the draft: {shown}")
    else:
        add("PASS", "added text", "no run of 3+ words in the page that is not in the draft")

    sh, ph = md_headings(source), [words(" ".join(h)) for h in doc.headings]
    pos, absent = 0, []
    for h in sh:
        try:
            pos = ph.index(h, pos) + 1
        except ValueError:
            absent.append(" ".join(h))
    add("FAIL" if absent else "PASS", "headings",
        f"{len(absent)} of {len(sh)} draft headings are missing or out of order: " + "; ".join(absent[:4]) if absent
        else f"all {len(sh)} draft headings are page headings, in order")
    tables = len(re.findall(r"^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$", source, re.M))
    items = len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+", source, re.M))
    add("INFO", "structure", f"draft: {tables} table(s), {items} list item(s); page: {doc.counts.get('table', 0)} table(s), "
                             f"{doc.counts.get('li', 0)} <li> (informational: tiles and cards may re-shape lists)")
    return out


def report(answer_path, page_bytes, shell_path, shell_bytes, source_path, source_bytes, checks):
    when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    n = lambda st: sum(1 for c in checks if c[0] == st)
    parity = "CHECKED against the draft above" if source_bytes is not None else "NOT CHECKED (no --source)"
    lines = ["DOC FIDELITY PROOF",
             f"answer file  : {answer_path}", f"answer sha256: {sha(page_bytes)}",
             f"source file  : {source_path if source_bytes is not None else '(none given)'}",
             f"source sha256: {sha(source_bytes) if source_bytes is not None else '-'}",
             f"house shell  : {shell_path}", f"shell sha256 : {sha(shell_bytes)}",
             f"checked at   : {when}",
             "proves       : the checks below ran on exactly the answer sha256 above",
             "does NOT     : prove the draft itself was right; with no --source it says nothing about content", ""]
    for st, name, detail in checks:
        lines.append(f"{st:4} {name:15} {detail}")
    lines += ["", f"SUMMARY: passed {n('PASS')} | failed {n('FAIL')} | info {n('INFO')} | content parity: {parity}"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prove an HTML-ified doc keeps the house shell and (with --source) its draft.")
    ap.add_argument("answer", help="the HTML document (a saved answer or a file)")
    ap.add_argument("--source", help="the markdown draft it was converted from; enables content parity")
    ap.add_argument("--shell", default=DEFAULT_SHELL, help="the house shell (default: references/html-doc-shell.html)")
    args = ap.parse_args(argv)
    try:
        page_b = open(args.answer, "rb").read()
        shell_b = open(args.shell, "rb").read()
        src_b = open(args.source, "rb").read() if args.source else None
    except OSError as e:
        print(f"ERROR: cannot read {e.filename}: {e.strerror}", file=sys.stderr)
        return 2
    checks = run_checks(page_b.decode("utf-8", "replace"), shell_b.decode("utf-8", "replace"),
                        src_b.decode("utf-8", "replace") if src_b is not None else None)
    sys.stdout.write(report(args.answer, page_b, args.shell, shell_b, args.source, src_b, checks))
    if any(c[0] == "FAIL" for c in checks):
        return 1
    return 0 if src_b is not None else 3


if __name__ == "__main__":
    sys.exit(main())
