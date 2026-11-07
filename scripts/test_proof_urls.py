#!/usr/bin/env python3
"""Probes for proof_urls.py, the tool that turns "the citations check out" into a file.

The URLs it requests come from MODEL OUTPUT, so the first job of these tests is that it cannot be
steered at an internal address: a loopback URL must produce ZERO requests at the server. The rest
pin the classification (only a 2xx is proof; a 403 proves nothing; a 404 is a finding) and the
report (it names the exact answer it checked, and says what it does not prove).

Pure stdlib. A local HTTP server stands in for the web; nothing leaves the machine.

    python3 -B scripts/test_proof_urls.py
"""
import contextlib
import hashlib
import importlib.util
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("proof_urls", os.path.join(_HERE, "proof_urls.py"))
pu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pu)

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


# ---- a local web ---------------------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *a):
        pass

    def _send(self, code, headers=None, body=b""):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self):
        H.hits.append((self.command, self.path))
        p = self.path
        if p == "/ok":
            self._send(200, body=b"ok")
        elif p == "/moved":
            self._send(301, {"Location": "/ok"})
        elif p == "/moved404":
            self._send(301, {"Location": "/gone"})
        elif p == "/gone":
            self._send(404)
        elif p == "/410":
            self._send(410)
        elif p == "/403":
            self._send(403)
        elif p == "/429":
            self._send(429)
        elif p == "/503":
            self._send(503)
        elif p == "/nohead":
            self._send(405 if self.command == "HEAD" else 200, body=b"ok")
        elif p == "/head404":
            self._send(404 if self.command == "HEAD" else 200, body=b"ok")
        elif p == "/slow":
            time.sleep(2)
            self._send(200)
        elif p == "/loop":
            self._send(302, {"Location": "/loop"})
        elif p == "/to-file":
            self._send(302, {"Location": "file:///etc/passwd"})
        elif p.startswith("/c") and p[2:].isdigit():       # /c0 -> /c1 -> ... -> /c6 -> 200
            n = int(p[2:])
            self._send(200) if n >= 6 else self._send(302, {"Location": f"/c{n + 1}"})
        else:
            self._send(404)

    do_GET = do_HEAD = _serve


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def one(path, timeout=5):
    return pu.check(BASE + path, timeout=timeout, allow_private=True)


# ---- classification (the server is loopback, so these run with the tests-only allow_private) --------
r = one("/ok")
check(r["kind"] == "resolved" and r["status"] == 200 and r["how"] == "HEAD", "a 200: resolved, by HEAD")
r = one("/moved")
check(r["kind"] == "resolved" and r["final"].endswith("/ok"), "a 301 to a 200: resolved, and the final URL is shown")
r = one("/moved404")
check(r["kind"] == "missing" and r["status"] == 404, "a 301 to a 404: missing")
check(one("/gone")["kind"] == "missing" and one("/410")["kind"] == "missing" and one("/410")["status"] == 410,
      "404 and 410: missing")
H.hits.clear()
r = one("/403")
check(r["kind"] == "unverified" and r["status"] == 403 and [m for m, _ in H.hits] == ["HEAD", "GET"],
      "a 403: UNVERIFIED (the host answered, the path is not confirmed); HEAD then GET were tried")
check(one("/429")["kind"] == "unverified", "a 429: unverified")
H.hits.clear()
r = one("/nohead")
check(r["kind"] == "resolved" and r["how"] == "GET" and [m for m, _ in H.hits] == ["HEAD", "GET"],
      "a server that refuses HEAD with 405: retried with GET, then resolved")
r = one("/503")
check(r["kind"] == "error" and "server error" in r["note"], "a 503: error")
t0 = time.time()
r = one("/slow", timeout=0.5)
check(r["kind"] == "error" and "timed out" in r["note"] and time.time() - t0 < 4, "a timeout: error, and it does not hang")
t0 = time.time()
r = one("/loop")
check(r["kind"] == "error" and time.time() - t0 < 4, "a redirect loop: error, and it does not hang")
check(one("/c1")["kind"] == "resolved", "5 redirects: followed")
r = one("/c0")
check(r["kind"] == "error" and "redirect" in r["note"], "6 redirects: error (the limit is 5)")
r = one("/to-file")
check(r["kind"] == "error" and r["status"] != 200, "a redirect to a file:// URL: error, never followed")

r = pu.check("https://this-host-does-not-exist.invalid/x", timeout=5)
check(r["kind"] == "error" and "DNS" in r["note"], "a hostname that does not resolve: error, DNS lookup failed")

# ---- Gemini grounding redirects expire: unverified by nature, and never requested ----------------------------
def no_request(*a, **k):
    raise AssertionError("a request was made")


_old_request = pu._request
pu._request = no_request
try:
    g = pu.check("https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQF1EGoH6nptk", allow_private=True)
    made = False
except AssertionError:
    g, made = None, True
pu._request = _old_request
check(not made and g["kind"] == "unverified" and "expires" in g["note"],
      "a vertexaisearch grounding-redirect token: UNVERIFIED (not 'missing'), and no request is made")
pu._request = lambda url, method, timeout, allow_private: (404, url)
r = pu.check("https://vertexaisearch.cloud.google.com/other/path", timeout=5, allow_private=True)
r2 = pu.check("https://vertexaisearch.cloud.google.com.evil.example/grounding-api-redirect/x", timeout=5, allow_private=True)
pu._request = _old_request
check(r["kind"] == "missing" and r2["kind"] == "missing",
      "...only that exact host and path: a different path, or a lookalike host, is requested like any other")
H.hits.clear()
r = one("/head404")
check(r["kind"] == "resolved" and r["how"] == "GET" and [m for m, _ in H.hits] == ["HEAD", "GET"],
      "a server that 404s a HEAD but serves the page: retried with GET, then resolved (not called missing)")
H.hits.clear()
r = one("/gone")
check(r["kind"] == "missing" and [m for m, _ in H.hits] == ["HEAD", "GET"], "a real 404 is confirmed with a GET before it is called missing")

# ---- where a request ENDS UP matters (found on the real answers: siftaudio.com -> a domain-for-sale page) ----
def judged(url, final, kind="unverified", note="host answered but refuses automated requests"):
    res = {"url": url, "kind": kind, "status": 403, "how": "GET", "final": final, "note": note}
    pu.judge_redirect(res)
    return res


r = judged("https://siftaudio.com", "https://www.hugedomains.com/domain_profile.cfm?d=siftaudio.com")
check(r["kind"] == "missing" and "domain-for-sale" in r["note"],
      "a 403 from a HugeDomains sale page is MISSING, not 'the host refuses bots' (the real siftaudio.com case)")
check(judged("https://x.example", "https://sub.sedo.com/p", kind="resolved", note="")["kind"] == "missing",
      "a 200 from a domain-for-sale page is still missing: the cited site is not there")
check(judged("https://x.example", "https://nothugedomains.com/p")["kind"] == "unverified",
      "a host that merely ENDS like a marketplace name is not one (nothugedomains.com)")
r = judged("https://example.com/a", "https://www.example.com/a", kind="resolved", note="")
check(r["kind"] == "resolved" and r["note"] == "", "example.com -> www.example.com: no note (same site)")
r = judged("https://example.com/a", "https://other.org/a", kind="resolved", note="")
check(r["kind"] == "resolved" and "different domain (other.org)" in r["note"], "a redirect to another domain: still resolved, with a visible note")
r = judged("https://example.com/a", "https://other.org/a")
check(r["kind"] == "unverified" and r["note"].startswith("host answered") and "different domain" in r["note"],
      "...and the note is appended to an existing one, not a replacement")
r = judged("https://example.com/a", None, kind="resolved", note="")
check(r["kind"] == "resolved" and r["note"] == "", "no final URL: nothing to judge")

_old_request = pu._request
pu._request = lambda url, method, timeout, allow_private: (403, "https://www.hugedomains.com/domain_profile.cfm?d=siftaudio.com")
r = pu.check("https://siftaudio.com", timeout=5, allow_private=True)
pu._request = _old_request
check(r["kind"] == "missing" and r["status"] == 403, "check() end to end: the same case is reported missing, with the status that was seen")

# ---- the guard: URLs from model output cannot reach internal addresses ---------------------------------
def blocked(url, resolve=None):
    try:
        pu.guard(url) if resolve is None else pu.guard(url, resolve)
        return False
    except pu.Blocked:
        return True


for what, url in (("loopback", "http://127.0.0.1:9/"), ("localhost", "http://localhost/"),
                  ("cloud metadata", "http://169.254.169.254/latest/meta-data/"), ("IPv6 loopback", "http://[::1]/"),
                  ("10/8", "http://10.1.2.3/"), ("192.168/16", "http://192.168.0.1/"), ("172.16/12", "http://172.16.5.5/"),
                  ("carrier-grade NAT", "http://100.64.0.1/"), ("IPv4-mapped loopback", "http://[::ffff:127.0.0.1]/"),
                  ("link-local IPv6", "http://[fe80::1]/"), ("0.0.0.0", "http://0.0.0.0/")):
    check(blocked(url), f"guard refuses {what}")
check(blocked("file:///etc/passwd") and blocked("ftp://example.com/") and blocked("gopher://example.com/"),
      "guard refuses file:, ftp: and gopher:")
check(blocked("http://user:secret@8.8.8.8/"), "guard refuses a URL that carries credentials")
check(not blocked("http://8.8.8.8/") and not blocked("https://1.1.1.1/dns"), "guard lets a public IP literal through")
public = lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", port))]
mixed = lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.7", port))]
check(not blocked("https://docs.example/x", public), "a hostname that resolves to a public address: allowed")
check(blocked("https://rebind.example/x", mixed), "a hostname with ANY non-public address among its answers: refused")

req = urllib.request.Request("http://93.184.216.34/")
try:
    pu._Redirects(False).redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/")
    ok = False
except pu.Blocked:
    ok = True
check(ok, "a redirect from a public URL to a metadata address is refused (every hop is checked)")
try:
    pu._Redirects(False).redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
    ok = False
except pu.Blocked:
    ok = True
check(ok, "...and to a file: URL")

# ---- the CLI cannot be steered at the machine itself: ZERO requests ---------------------------------------
tmp = tempfile.mkdtemp(prefix="pu-")
loop_file = os.path.join(tmp, "answer.txt")
open(loop_file, "w").write(f"The docs are at {BASE}/ok and {BASE}/gone.\n")
H.hits.clear()
cli = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "proof_urls.py"), loop_file],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(H.hits == [], f"CLI pointed at loopback URLs: the server received {len(H.hits)} request(s) (must be 0)")
check(cli.returncode == 1 and cli.stdout.count("blocked:") == 2 and "ERROR" in cli.stdout.upper(),
      "...and the report says both were blocked, exit 1")

empty_file = os.path.join(tmp, "none.txt")
open(empty_file, "w").write("There are no links here, only ftp://example.com/x and mailto:a@b.c\n")
cli = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "proof_urls.py"), empty_file],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(cli.returncode == 2 and cli.stdout == "" and "no http" in cli.stderr, "no URL found: exit 2 and NOTHING on stdout (an empty proof cannot be made)")
cli = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "proof_urls.py"), os.path.join(tmp, "missing.txt")],
                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(cli.returncode == 2 and cli.stdout == "", "an unreadable file: exit 2, nothing on stdout")

# ---- extraction ---------------------------------------------------------------------------------------------
ex = pu.extract_urls
check(ex("[docs](https://a.example/x)") == ["https://a.example/x"], "a markdown link")
check(ex("[https://a.example/p](https://a.example/p)") == ["https://a.example/p"], "a markdown link whose text is the URL: one URL")
check(ex("see **https://a.example/y** now") == ["https://a.example/y"], "bold markdown around a URL")
check(ex("https://a.example/avspeech*\nhttps://b.example/c***") == ["https://a.example/avspeech", "https://b.example/c"],
      "trailing * from emphasis (the real answers had these)")
check(ex("clone https://github.com/x/pyaaf2.") == ["https://github.com/x/pyaaf2"], "a sentence-ending period")
check(ex("(see https://a.example/w)") == ["https://a.example/w"], "an unbalanced closing parenthesis")
check(ex("https://en.wikipedia.org/wiki/Foo_(bar)") == ["https://en.wikipedia.org/wiki/Foo_(bar)"], "a balanced parenthesis is kept")
check(ex("https://a.example/?a=1&amp;b=2") == ["https://a.example/?a=1&b=2"], "&amp; is unescaped")
check(ex("https://a.example/1 https://a.example/1 https://a.example/2") == ["https://a.example/1", "https://a.example/2"],
      "duplicates are dropped, order is kept")
check(ex("ftp://x.example/a mailto:a@b.c javascript:alert(1) https://") == [], "non-http schemes and bare https:// are ignored")
check(ex("trailing comma https://a.example/q, and more") == ["https://a.example/q"], "a trailing comma")

# ---- the report ------------------------------------------------------------------------------------------------
res = pu.run_checks([BASE + "/ok", BASE + "/moved", BASE + "/gone", BASE + "/403"], timeout=5, allow_private=True)
answer = "the answer text\n".encode()
text, errors = pu.report("data/outputs/x.txt", hashlib.sha256(answer).hexdigest(), res, ["https://over.example/limit"], 15)
check(hashlib.sha256(answer).hexdigest() in text, "the report names the sha256 of the exact answer it checked")
check("checked at" in text and "does NOT" in text and "link rot" not in text.split("does NOT")[0],
      "...the time, and a plain statement of what it does not prove")
check("RESOLVED" in text and "MISSING" in text and "UNVERIFIED" in text, "...one line per URL with its class")
check(f"-> {BASE}/ok" in text, "...a redirect shows where it ended")
check("NOT CHECKED (over the --max-urls limit): https://over.example/limit" in text, "...an over-limit URL is listed, not silently dropped")
check("SUMMARY: resolved 2 of 5 | unverified 1 | missing 1 | errors 1" in text and errors == 1,
      "...the summary counts add up (an unchecked URL counts as an error)")
check(pu.display("https://user:pw@h.example/p") == "https://***@h.example/p", "display() redacts credentials")
check("\x1b" not in pu.display("https://h.example/\x1b[31mred") and len(pu.display("https://h.example/" + "a" * 900)) <= 300,
      "display() strips control characters and bounds the length")


# ---- exit codes (main with canned results) ----------------------------------------------------------------------
def run_main(kinds):
    f = os.path.join(tmp, "a.txt")
    open(f, "w").write(" ".join(f"https://u{i}.example/p" for i in range(len(kinds))))
    canned = [{"url": f"https://u{i}.example/p", "kind": k, "status": 200, "how": "HEAD", "final": None, "note": "", "seconds": 0.1}
              for i, k in enumerate(kinds)]
    old = pu.run_checks
    pu.run_checks = lambda urls, timeout=15, allow_private=False, workers=4: canned
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return pu.main([f])
    finally:
        pu.run_checks = old


check(run_main(["resolved", "resolved"]) == 0, "exit 0: every URL resolved")
check(run_main(["resolved", "missing"]) == 1, "exit 1: any missing")
check(run_main(["resolved", "error"]) == 1, "exit 1: any error")
check(run_main(["resolved", "unverified"]) == 3, "exit 3: unverified only (not proof, not a failure)")
check(run_main(["unverified", "missing"]) == 1, "exit 1: a missing URL outranks unverified")

# ---- --max-urls is applied by the CLI path, and what it skips is reported --------------------------------------------
seen_urls = []
f4 = os.path.join(tmp, "four.txt")
open(f4, "w").write("https://a.example/1 https://a.example/2 https://a.example/3 https://a.example/4")
old = pu.run_checks
pu.run_checks = lambda urls, timeout=15, allow_private=False, workers=4: (seen_urls.extend(urls) or [
    {"url": u, "kind": "resolved", "status": 200, "how": "HEAD", "final": None, "note": "", "seconds": 0.1} for u in urls])
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        code = pu.main([f4, "--max-urls", "2"])
finally:
    pu.run_checks = old
check(seen_urls == ["https://a.example/1", "https://a.example/2"] and buf.getvalue().count("NOT CHECKED") == 2 and code == 1,
      "--max-urls 2 on 4 URLs: only 2 are requested, 2 are listed as NOT CHECKED, and the exit is 1 (an incomplete proof)")

# ---- the proof attaches ---------------------------------------------------------------------------------------------
led = os.path.join(tmp, "usage.jsonl")
open(led, "w").write('{"ts":"2026-11-01T10:00:01","run_id":"aaaaaaaaaaaa","script":"gemini","model":"m","tag":"research"}\n')
proof = os.path.join(tmp, "proof.txt")
open(proof, "w").write(text)
v = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "verdict.py"), "good", "ok", "--run-id", "aaaaaaaaaaaa",
                    "--tag", "research", "--evidence", proof],
                   env=dict(os.environ, SMITH_LEDGER=led, SMITH_VERDICT_DB=os.path.join(tmp, "v.db")),
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)
check(v.returncode == 0 and "evidence/" in v.stdout, "the report is accepted by `verdict.py --evidence`")

srv.shutdown()
print("\nall passed" if all_pass else "\nFAILURES above")
sys.exit(0 if all_pass else 1)
