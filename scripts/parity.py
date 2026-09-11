#!/usr/bin/env python3
"""Did the fleet's HTML keep every word of the markdown draft?

WHY THIS EXISTS. Playbook 5 says "parity check" and shipped no checker, so it got
hand-rolled per use — and the obvious implementation is WRONG in a way that looks
right. It needs TWO mistakes together, which is why it survives a read: strip tags
to a SPACE (splitting every word an inline <code> or <strong> touches) and then
merely COLLAPSE whitespace rather than remove it, and the halves no longer meet.
Measured: that pair reported 10 false positives out of 101 real units; fixing
either half alone drops it to 0. A checker that cries wolf gets ignored, which is
worse than no checker, so this does both — tags strip to nothing AND the compare
ignores whitespace entirely.

The failure this guards against is real and measured: at default temperature a
model dropped whole sentences and compressed explanations while obeying every
other instruction (verdict 2026-07-12). Use --temperature 0 AND check.

    python3 parity.py draft.md out.html

Exit 0 = every unit survived. Exit 1 = something was dropped. Exit 2 = bad usage.
"""
import html
import re
import sys


def html_text(raw: str) -> str:
    """Visible text of a page. Tags strip to NOTHING — see the docstring."""
    out = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    out = re.sub(r"<!--.*?-->", " ", out, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", out))


def squash(s: str) -> str:
    """Whitespace-free, case-folded, with the punctuation conversion normalises."""
    for a, b in (("—", "-"), ("–", "-"), ("’", "'"),
                 ("“", '"'), ("”", '"'), ("…", "...")):
        s = s.replace(a, b)
    return re.sub(r"\s+", "", s).lower()


def units(md: str) -> list[str]:
    """The draft as checkable pieces: table cells, and sentences elsewhere."""
    body = re.sub(r"`([^`]*)`", r"\1", md)
    body = re.sub(r"\*\*([^*]*)\*\*", r"\1", body)
    body = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", body)
    body = re.sub(r"^\s*#+ ", "", body, flags=re.M)
    body = re.sub(r"^\s*[-*+] ", "", body, flags=re.M)

    found: list[str] = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("|"):
            found += [c.strip() for c in line.strip("|").split("|")
                      if c.strip() and set(c.strip()) != {"-"}]
        else:
            found += [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
    return found


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    md_path, html_path = sys.argv[1], sys.argv[2]
    md = open(md_path, encoding="utf-8").read()
    page = html_text(open(html_path, encoding="utf-8").read())
    haystack = squash(page)

    checked = [u for u in units(md) if len(squash(u)) > 12]
    missing = [u for u in checked if squash(u) not in haystack]

    src_chars, out_chars = len(squash(md)), len(haystack)
    print(f"{len(checked)} units checked, {len(missing)} missing "
          f"(source {src_chars} chars, page {out_chars})")

    # Text much LONGER than the draft means invented content, which parity alone
    # cannot see — every draft unit can be present in a page that also made things up.
    if out_chars > src_chars * 1.15:
        print(f"  ⚠ page text is {out_chars / src_chars:.2f}x the draft — "
              f"read it for invented content", file=sys.stderr)

    for m in missing:
        print(f"  MISSING: {m[:160]}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
