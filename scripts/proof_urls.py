#!/usr/bin/env python3
"""Proof that the URLs in a saved answer exist: request each one and report what came back.

    python3 proof_urls.py data/outputs/2026-09-25/<answer>.txt > proof.txt
    python3 verdict.py good "<same note>" --tag research --ts <run ts> ... --evidence proof.txt

Why: the failure the research tier exists to catch is a FABRICATED URL (a real URL pattern, a made
up path). A grade that says "citations check out" is a claim; this produces the check. It writes the
report to stdout, so the proof is a file you can attach with `verdict.py --evidence`.

What it proves, and what it does not. It proves each URL was requested just now and shows the final
status. It does NOT prove the page says what the answer claims, and a URL that fails today may have
moved since the answer was written (link rot), so read the missing ones before you grade.

  resolved    the final status was 2xx (after at most 5 redirects)
  missing     404 or 410
  unverified  401/403/429/451/999: the host answered but refuses automated requests, so the path is
              NOT confirmed; this counts as neither proof nor failure
  error       DNS failure, timeout, TLS error, 5xx, a redirect loop, or a URL this tool refused

Exit code: 0 every URL resolved; 1 any missing or error; 3 none missing/error but some unverified;
2 no URL found or unreadable file (nothing is printed to stdout, so an empty proof cannot be made).

The URLs come from MODEL OUTPUT, so they are untrusted. Only http/https is requested, a URL whose
host resolves to a non-public address (loopback, private, link-local, cloud metadata) is refused,
and that check is repeated on every redirect hop. No cookies, no credentials, headers only (HEAD,
falling back to GET without reading the body). Known gap: the check resolves the name and the
request resolves it again, so a hostile DNS server could answer differently the second time.
Python stdlib only.
"""
import argparse
import datetime
import hashlib
import html
import ipaddress
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "agent-smith-proof-urls/1 (checks that URLs cited in an answer exist)"
MAX_REDIRECTS = 5
URL_RE = re.compile(r"""https?://[^\s<>"'`\[\]]+""", re.I)
TRAIL = ".,;:!?*\"'>}"          # markdown and sentence punctuation peeled off the end of a URL
UNVERIFIED = {401, 403, 429, 451, 999}
MISSING = {404, 410}
# Marketplaces that sell or park domains. A cited URL that ends up on one of these is not the site
# the answer meant. Found 2026-11-07: siftaudio.com, cited as a product's site, redirected to a
# HugeDomains sale page that answered 403 to a bot, which a plain classifier reads as "the host
# refuses automated requests" and buries the finding.
PARKING = ("hugedomains.com", "sedo.com", "dan.com", "afternic.com", "squadhelp.com", "atom.com",
           "brandbucket.com", "bodis.com", "parkingcrew.net", "above.com", "undeveloped.com")


class Blocked(Exception):
    """A URL this tool will not request, and why."""


def extract_urls(text):
    """http(s) URLs in `text`, in order, de-duplicated. Peels markdown emphasis (`**url**`),
    sentence punctuation and an unbalanced closing parenthesis (`[x](url)`) off the end, and
    keeps a balanced one (`.../Foo_(bar)`)."""
    out = []
    for m in URL_RE.finditer(text):
        u = html.unescape(m.group(0))
        while u:
            if u[-1] in TRAIL:
                u = u[:-1]
            elif u[-1] == ")" and u.count(")") > u.count("("):
                u = u[:-1]
            else:
                break
        if len(u) > len("https://x") and u not in out:
            out.append(u)
    return out


def display(url):
    """The URL as printed in the report: no credentials, no control characters, bounded length."""
    u = re.sub(r"//[^/@\s]*@", "//***@", url)
    u = "".join(c if c.isprintable() else "?" for c in u)
    return u if len(u) <= 300 else u[:297] + "..."


def _is_public(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    return (getattr(ip, "ipv4_mapped", None) or ip).is_global


def guard(url, resolve=socket.getaddrinfo):
    """Raise Blocked unless `url` is http(s), carries no credentials and resolves only to public
    addresses. A DNS failure is not Blocked: it propagates as socket.gaierror."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise Blocked(f"scheme {p.scheme!r} is not http or https")
    if not p.hostname:
        raise Blocked("no host")
    if p.username or p.password:
        raise Blocked("the URL carries credentials; not requested")
    port = p.port or (443 if p.scheme == "https" else 80)
    addrs = {i[4][0] for i in resolve(p.hostname, port, proto=socket.IPPROTO_TCP)}
    bad = sorted(a for a in addrs if not _is_public(a))
    if bad:
        raise Blocked(f"{p.hostname} resolves to a non-public address ({bad[0]}); not requested")


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows at most MAX_REDIRECTS hops and checks EVERY hop with guard(): a public URL may
    redirect to localhost or a metadata address."""
    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_private=False):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _guard_if(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _guard_if(url, allow_private):
    if allow_private:       # tests only: the CLI never sets it. The scheme is still enforced.
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise Blocked("scheme is not http or https")
    else:
        guard(url)


def _request(url, method, timeout, allow_private):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirects(allow_private))
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.geturl()


def is_grounding_redirect(url):
    """Gemini's search grounding cites sources as opaque redirect tokens
    (vertexaisearch.cloud.google.com/grounding-api-redirect/...) that expire. Found 2026-11-07: two
    saved research answers whose every URL (21 and 10) is one of these, all 404 now. They are not
    fabricated, but the real source behind them can no longer be re-checked, so they are
    unverified by nature. Not even requested."""
    p = urllib.parse.urlsplit(url)
    return p.hostname == "vertexaisearch.cloud.google.com" and p.path.startswith("/grounding-api-redirect/")


def _site(host):
    """The last two labels of a hostname: a rough registrable domain, enough to tell
    www.example.com from a different site."""
    return ".".join((host or "").lower().split(".")[-2:])


def judge_redirect(res):
    """Adjust a result for WHERE the request ended up. A redirect to a domain-for-sale page makes
    the URL missing whatever status that page returned; any other change of domain is a note, not a
    verdict, because acquisitions and canonical domains are legitimate."""
    final, url = res.get("final"), res["url"]
    if not final or final == url:
        return
    fh, oh = urllib.parse.urlsplit(final).hostname, urllib.parse.urlsplit(url).hostname
    if not fh or not oh:
        return
    if any(fh == d or fh.endswith("." + d) for d in PARKING):
        res["kind"] = "missing"
        res["note"] = f"redirects to a domain-for-sale page ({fh}); the cited site is not there"
    elif _site(fh) != _site(oh):
        extra = f"redirected to a different domain ({fh})"
        res["note"] = f"{res['note']}; {extra}" if res["note"] else extra


def check(url, timeout=15, allow_private=False):
    """One result: {url, kind, status, how, seconds, final, note}."""
    t0, how = time.time(), "HEAD"
    res = {"url": url, "kind": "error", "status": None, "how": how, "final": None, "note": ""}
    if is_grounding_redirect(url):
        res.update(kind="unverified", how="-", seconds=0.0,
                   note="a Gemini search-grounding redirect: it expires, so the real source behind it "
                        "cannot be re-checked (not requested)")
        return res
    try:
        _guard_if(url, allow_private)
        try:
            status, final = _request(url, "HEAD", timeout, allow_private)
            if status in (400, 403, 404, 405, 501):    # many servers refuse or 404 a HEAD but allow GET
                how = "GET"
                status, final = _request(url, "GET", timeout, allow_private)
        except (Blocked, socket.gaierror, TimeoutError):
            raise
        except (urllib.error.URLError, OSError):       # some servers reset a HEAD: try once with GET
            how = "GET"
            status, final = _request(url, "GET", timeout, allow_private)
        res.update(status=status, how=how, final=final)
        if 200 <= status < 300:
            res["kind"] = "resolved"
        elif status in MISSING:
            res["kind"] = "missing"
        elif status in UNVERIFIED:
            res["kind"] = "unverified"
            res["note"] = "host answered but refuses automated requests; the path is not confirmed"
        elif 300 <= status < 400:
            res["note"] = f"redirect loop or more than {MAX_REDIRECTS} redirects"
        elif status >= 500:
            res["note"] = "server error"
        else:
            res["note"] = "unexpected status"
        judge_redirect(res)
    except Blocked as e:
        res["note"] = f"blocked: {e}"
    except socket.gaierror:
        res["note"] = "DNS lookup failed"
    except (TimeoutError, socket.timeout):
        res["note"] = f"timed out after {timeout}s"
    except urllib.error.URLError as e:
        res["note"] = f"{type(e.reason).__name__}: {e.reason}"[:120]
    except Exception as e:  # noqa: BLE001 — a proof tool reports, it does not crash
        res["note"] = f"{type(e).__name__}: {e}"[:120]
    res["how"], res["seconds"] = how, round(time.time() - t0, 1)
    return res


def run_checks(urls, timeout=15, allow_private=False, workers=4):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda u: check(u, timeout, allow_private), urls))


def report(path, sha, results, skipped, timeout):
    when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    n = lambda k: sum(1 for r in results if r["kind"] == k)
    lines = ["URL RESOLUTION PROOF",
             f"answer file  : {path}",
             f"answer sha256: {sha}",
             f"checked at   : {when}",
             f"method       : HEAD, falling back to GET (headers only); up to {MAX_REDIRECTS} redirects; "
             f"{timeout}s timeout; non-public addresses refused",
             "proves       : each URL below was requested just now; the final status is shown",
             "does NOT     : prove the page says what the answer claims. A URL that fails today may have",
             "               moved since the answer was written, so read the missing ones before you grade.",
             ""]
    for i, r in enumerate(results, 1):
        status = r["status"] if r["status"] is not None else "-"
        lines.append(f"{i:2}. {display(r['url'])}")
        tail = f"{r['kind'].upper():10} {status}  ({r['how']}, {r.get('seconds', 0)}s)"
        if r["final"] and r["final"] != r["url"]:
            tail += f"  -> {display(r['final'])}"
        lines.append(f"    {tail}" + (f"  [{r['note']}]" if r["note"] else ""))
    for u in skipped:
        lines.append(f"    NOT CHECKED (over the --max-urls limit): {display(u)}")
    err = n("error") + len(skipped)
    lines += ["", f"SUMMARY: resolved {n('resolved')} of {len(results) + len(skipped)} | "
              f"unverified {n('unverified')} | missing {n('missing')} | errors {err}"]
    return "\n".join(lines) + "\n", err


def main(argv=None):
    ap = argparse.ArgumentParser(description="Request every URL in a saved answer and print a proof.")
    ap.add_argument("answer", help="path of the saved answer (e.g. data/outputs/<date>/<run>.txt)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--max-urls", type=int, default=60)
    args = ap.parse_args(argv)
    try:
        raw = open(args.answer, "rb").read()
    except OSError as e:
        print(f"ERROR: cannot read {args.answer}: {e}", file=sys.stderr)
        return 2
    urls = extract_urls(raw.decode("utf-8", "replace"))
    if not urls:
        print(f"no http(s) URLs found in {args.answer}; no proof written", file=sys.stderr)
        return 2
    checked, skipped = urls[:args.max_urls], urls[args.max_urls:]
    results = run_checks(checked, args.timeout)
    text, errors = report(args.answer, hashlib.sha256(raw).hexdigest(), results, skipped, args.timeout)
    sys.stdout.write(text)
    missing = sum(1 for r in results if r["kind"] == "missing")
    unverified = sum(1 for r in results if r["kind"] == "unverified")
    if missing or errors:
        return 1
    return 3 if unverified else 0


if __name__ == "__main__":
    sys.exit(main())
