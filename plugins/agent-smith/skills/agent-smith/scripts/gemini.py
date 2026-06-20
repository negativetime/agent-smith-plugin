#!/usr/bin/env python3
"""
gemini.py — shell out to Google Gemini using the user's GEMINI_API_KEY.

Purpose: offload bulk / large-context / research work to Gemini so it doesn't
burn Claude tokens. Answer goes to STDOUT; model, token usage, and any grounding
sources go to STDERR (so captured output stays clean).

Pure stdlib — no pip installs required.

Examples:
  python3 gemini.py "Summarize the theory of relativity in 5 bullets" --model flash
  echo "long prompt text..." | python3 gemini.py --model pro
  python3 gemini.py --search "What changed in the latest macOS Tahoe release?"
  python3 gemini.py --file report.pdf "Extract every dollar figure as a markdown table"
  python3 gemini.py --file a.csv --file b.csv --json "Merge these and list duplicate rows"
  python3 gemini.py --list-models
"""
import argparse
import ast
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com"
INLINE_LIMIT = 15 * 1024 * 1024  # files larger than this go through the Files API

# Friendly aliases. Pass any real model name through unchanged.
ALIASES = {
    "flash": "gemini-flash-latest",
    "pro": "gemini-pro-latest",
    "flash-lite": "gemini-flash-lite-latest",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def api_key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        log("ERROR: GEMINI_API_KEY is not set. Three options: (1) get a free key at "
            "https://aistudio.google.com/apikey and `export GEMINI_API_KEY=...`; (2) skip the "
            "account and run local with `--backend ollama` (set up a model via "
            "scripts/setup_local_model.sh, e.g. Gemma for general or qwen2.5-coder for code); or "
            "(3) `--backend gemini-cli` to run Gemini on your Google login, no API key needed.")
        sys.exit(2)
    return k


def http(url, method="GET", data=None, headers=None, timeout=300):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(url, method="GET", body=None, headers=None, timeout=300, retries=4):
    """POST/GET JSON with retry/backoff on 429 + 5xx."""
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    payload = json.dumps(body).encode() if body is not None else None
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with http(url, method, payload, h, timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")
            if e.code in (429, 500, 503) and attempt < retries:
                log(f"  [retry] HTTP {e.code} (attempt {attempt + 1}/{retries}); waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            log(f"ERROR: HTTP {e.code} from Gemini:\n{err[:2000]}")
            sys.exit(1)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                log(f"  [retry] network error: {e}; waiting {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            log(f"ERROR: network failure talking to Gemini: {e}")
            sys.exit(1)


def guess_mime(path):
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime
    # Sniff: treat decodable bytes as plain text, else generic binary.
    try:
        with open(path, "rb") as f:
            f.read(2048).decode("utf-8")
        return "text/plain"
    except Exception:
        return "application/octet-stream"


def upload_file(path, key):
    """Resumable Files API upload for large files. Returns (uri, mime)."""
    with open(path, "rb") as f:
        data = f.read()
    mime = guess_mime(path)
    name = os.path.basename(path)
    start = http(
        f"{BASE}/upload/v1beta/files?key={key}",
        method="POST",
        data=json.dumps({"file": {"display_name": name}}).encode(),
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    upload_url = start.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        log("ERROR: Files API did not return an upload URL.")
        sys.exit(1)
    with http(
        upload_url,
        method="POST",
        data=data,
        headers={
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(len(data)),
        },
    ) as r:
        info = json.loads(r.read().decode())
    fobj = info["file"]
    fname, uri, state = fobj["name"], fobj["uri"], fobj.get("state", "ACTIVE")
    while state == "PROCESSING":
        time.sleep(2)
        with http(f"{BASE}/v1beta/{fname}?key={key}") as r:
            fobj = json.loads(r.read().decode())
        state = fobj.get("state", "ACTIVE")
    if state == "FAILED":
        log(f"ERROR: Gemini failed to process file {name}.")
        sys.exit(1)
    log(f"  [file] uploaded {name} ({len(data)} bytes) -> {uri}")
    return uri, fobj.get("mimeType", mime)


def make_file_part(path, key):
    size = os.path.getsize(path)
    mime = guess_mime(path)
    if size <= INLINE_LIMIT:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return {"inlineData": {"mimeType": mime, "data": b64}}
    uri, mime = upload_file(path, key)
    return {"fileData": {"mimeType": mime, "fileUri": uri}}


def extract_text(candidate):
    parts = candidate.get("content", {}).get("parts", []) or []
    out = []
    for p in parts:
        if p.get("thought"):  # skip internal thinking parts
            continue
        if "text" in p:
            out.append(p["text"])
    return "".join(out)


def grounding_sources(candidate):
    md = candidate.get("groundingMetadata", {})
    chunks = md.get("groundingChunks", []) or []
    srcs = []
    for c in chunks:
        web = c.get("web", {})
        if web.get("uri"):
            srcs.append((web.get("title", "").strip(), web["uri"]))
    return srcs


def resolve_fm_helper():
    """Find the Apple FM sidecar binary: $FM_HELPER, then next to this script, then PATH."""
    env = os.environ.get("FM_HELPER")
    if env and os.path.exists(env):
        return env
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fm_helper")
    if os.path.exists(local):
        return local
    return shutil.which("fm_helper")


def call_fm(prompt, system, temperature):
    """Apple Foundation Models, on-device (macOS 26+). Free, offline, private, no quota."""
    helper = resolve_fm_helper()
    if not helper:
        log("ERROR: Apple FM helper (fm_helper) not found next to this script or on PATH. "
            "Set FM_HELPER=/path/to/fm_helper. Requires macOS 26+ with Apple Intelligence on.")
        sys.exit(2)
    req = {"messages": [{"role": "user", "content": prompt}]}
    if system:
        req["system"] = system
    if temperature is not None:
        req["temperature"] = temperature
    try:
        out = subprocess.run([helper], input=json.dumps(req).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except Exception as e:
        log(f"ERROR: could not run fm_helper: {e}")
        sys.exit(1)
    if out.returncode != 0:
        log(f"ERROR: fm_helper exited {out.returncode}: {out.stderr.decode('utf-8', 'replace')[:500]}")
        sys.exit(1)
    try:
        d = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        log(f"ERROR: unexpected fm_helper output: {out.stdout.decode('utf-8', 'replace')[:500]}")
        sys.exit(1)
    if d.get("error"):
        log(f"ERROR: Apple FM: {d['error']}")
        sys.exit(1)
    log("\n--- fm meta ---")
    log("backend: apple-foundation-models (on-device · free · offline · private)")
    return d.get("answer", "")


def call_ollama(prompt, system, temperature, model, max_tokens):
    """Local model via Ollama (http://localhost:11434). Free, unlimited, offline."""
    model = model or os.environ.get("OLLAMA_MODEL") or "qwen3-coder:30b"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "stream": False}
    opts = {}
    if temperature is not None:
        opts["temperature"] = temperature
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    if opts:
        body["options"] = opts
    req = urllib.request.Request("http://localhost:11434/api/chat", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        log(f"ERROR: Ollama HTTP {e.code}: {msg[:300]}. Is '{model}' pulled? Try: ollama pull {model}")
        sys.exit(1)
    except urllib.error.URLError as e:
        log(f"ERROR: can't reach Ollama ({e}). Start it: `ollama serve`, then `ollama pull {model}`.")
        sys.exit(1)
    text = resp.get("message", {}).get("content", "")
    log("\n--- ollama meta ---")
    log(f"backend: ollama  model: {model}  (local · free · unlimited)")
    pe, ec = resp.get("prompt_eval_count"), resp.get("eval_count")
    if pe is not None or ec is not None:
        log(f"tokens: prompt={pe} output={ec}")
    return text


# --- Code pre-flight (pure stdlib, no model calls) -------------------------------
#
# DESIGN DECISION: SYNTAX-CHECK ONLY, never EXECUTE, by default.
#
# When an offload is a code-generation task, we want to catch obviously-broken
# drafts (syntax errors) at the backend before they reach the orchestrator, so a
# broken draft can be auto-retried or flagged instead of wasting a verify pass.
#
# We do this by PARSING the code (ast.parse), NOT by running it. Running
# model-generated code here would be executing untrusted input on the user's
# machine with the user's privileges, env vars, network, and filesystem — a
# classic arbitrary-code-execution hole. A "draft" can contain anything: an
# `os.system("rm -rf ...")` at import time, a network exfil call, an infinite
# loop, or a crash. ast.parse touches none of that: it only builds the syntax
# tree and reports SyntaxError, with zero side effects. Real execution (sandboxed
# subprocess, resource limits, no network) could be a SEPARATE, EXPLICIT opt-in
# later, but it must default OFF. Syntax-check is the safe, useful 80%: it catches
# the failure mode we actually see from LLM code drafts (truncation, unbalanced
# brackets, stray prose), and it cannot harm the host.

def extract_code(text: str) -> str:
    """Strip a single leading/trailing Markdown code fence if present, else return as-is.

    Handles ```python ... ```, ```py ... ```, and bare ``` ... ``` fences. If the
    text isn't fenced (or is malformed), it's returned unchanged so the caller can
    still attempt to parse it.
    """
    if text is None:
        return ""
    s = text.strip()
    if not s.startswith("```"):
        return text
    lines = s.splitlines()
    # Drop the opening fence line (```python / ```py / ``` plus any info string).
    lines = lines[1:]
    # Drop the closing fence line if the block is properly closed.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def preflight_python(code: str) -> tuple:
    """Syntax-check (NOT execute) Python source after stripping any code fence.

    Returns (True, "") if it parses cleanly, else (False, "<SyntaxError msg + lineno>").
    """
    src = extract_code(code)
    try:
        ast.parse(src)
        return (True, "")
    except SyntaxError as e:
        msg = e.msg or "invalid syntax"
        line = e.lineno if e.lineno is not None else "?"
        return (False, f"{msg} (line {line})")


# First-person "I did / will do an action" phrases that mark an agentic non-answer
# (the CLI describing a file action instead of returning the requested content).
_ACTION_PHRASES = (
    "i have created", "i have written", "i have implemented", "i have added",
    "i have updated", "i have modified", "i have generated", "i have edited",
    "i created", "i wrote", "i implemented", "i added", "i've created",
    "i've implemented", "i've written", "has been created", "have been created",
    "has been written", "have been written", "has been implemented",
    "i will now", "i'll now", "i will wait", "i am now", "i'll wait",
    "i understand you want", "i understand that you want",
    "let me know if", "if you'd like me to", "if you would like me to",
    "would you like me to", "let me know whether",
)
_FILE_TEST_PHRASES = (
    "already exist", "already exists", "the file", "the files", ".py file",
    "tests pass", "tests passed", "all tests", "test file", "have been verified",
    "has been verified", "and verified",
)
_CODE_TOKENS = (
    "def ", "class ", "import ", "lambda ", "=>", "#include", "</", "/>",
    "::", "->", ":=", "```", "    return ", "\treturn ", "; ", " === ",
)
_DECL_RE = re.compile(
    r"(?m)^\s*(?:function|const|let|var|public|private|static)\s+\w+\s*[(=]")


def _looks_like_content(resp):
    """True if the text carries code/markup/structured substance worth keeping."""
    s = resp.strip()
    low = s.lower()
    if any(tok in low for tok in _CODE_TOKENS):
        return True
    if _DECL_RE.search(s):
        return True
    if (s.startswith("{") and s.rstrip().endswith("}")) or (
            s.startswith("[") and s.rstrip().endswith("]")):
        return True
    if re.search(r"(?m)^\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|\|)", s):
        return True
    return False


def detect_agentic_nonanswer(resp):
    """Flag a Gemini-CLI reply that DESCRIBES AN ACTION instead of containing the requested
    content (e.g. 'I created the file...'). Conservative: tuned for low false positives."""
    if not resp:
        return False
    s = resp.strip()
    if not s:
        return False
    low = s.lower()
    if _looks_like_content(s):
        return False
    if len(s) > 600 or s.count("\n") >= 6:
        return False
    if not any(p in low for p in _ACTION_PHRASES):
        return False
    has_file_test = any(p in low for p in _FILE_TEST_PHRASES)
    starts_with_action = any(low.startswith(p) for p in _ACTION_PHRASES)
    return has_file_test or starts_with_action


def call_gemini_cli(prompt, system, temperature, model, preflight=False):
    """Drive the locally-installed Gemini CLI using its OWN auth (the OAuth/Google login),
    instead of the metered API key. The point: a logged-in CLI runs on your subscription /
    account quota, so you dodge the API free-tier rate limits (429s) entirely.

    By default the GEMINI_API_KEY is hidden from the CLI's environment so it falls back to your
    Google login — run `gemini` once and pick "Login with Google" to set that up. Set
    GEMINI_CLI_USE_API_KEY=1 to let the CLI use the API key instead (defeats the purpose, but
    handy for testing). Text in, text out: --file and --search stay on the `gemini` backend.
    """
    binary = os.environ.get("GEMINI_CLI") or shutil.which("gemini")
    if not binary:
        log("ERROR: gemini CLI not found. Install it (`npm i -g @google/gemini-cli`) or set "
            "GEMINI_CLI=/path/to/gemini.")
        sys.exit(2)
    # The gemini CLI is an agentic CODER: left alone it may try to create files and report on the
    # action instead of returning the content. This directive (plus read-only `plan` mode) pins it
    # to plain text-generation behavior, reliably, across models.
    directive = ("Output only the requested content as plain text. Do not use any tools, and do not "
                 "create, edit, or read files. Do not describe what you did. Return just the answer.")
    body = f"{system}\n\n{prompt}" if system else prompt
    # Always pin a clean model. The CLI's OWN default is a tool-preview model that leaks its
    # tool-use "thinking" into the response field; a plain model returns just the answer. Map our
    # flash/pro aliases (default flash) and pass full CLI model names through unchanged.
    _cli_models = {"flash": "gemini-2.5-flash", "pro": "gemini-2.5-pro",
                   "flash-lite": "gemini-2.5-flash-lite"}
    cli_model = _cli_models.get(model or "flash", model)

    use_key = os.environ.get("GEMINI_CLI_USE_API_KEY", "").lower() in ("1", "true", "yes")
    env = dict(os.environ)
    if not use_key:  # force OAuth/subscription auth by hiding the key from the CLI
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            env.pop(k, None)
    # Run in a neutral temp dir so the CLI can't pick up a project's GEMINI.md/context or
    # touch real files (it's also in read-only `plan` mode).
    cwd = os.path.join(tempfile.gettempdir(), "agent-smith-gemini-cli")
    os.makedirs(cwd, exist_ok=True)

    def _invoke(text):
        """Run the CLI once with the given full prompt text; return (answer, mname, toks)."""
        cmd = [binary, "-p", text, "-o", "json", "--skip-trust", "--approval-mode", "plan",
               "-m", cli_model]
        # Deny ALL tools at the policy level — belt-and-suspenders with the directive + read-only
        # `plan` mode. The model then can't go agentic, and denied tools drop from its prompt.
        _policy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deny_all_tools.toml")
        if os.path.exists(_policy):
            cmd += ["--policy", _policy]
        try:
            out = subprocess.run(cmd, input="", capture_output=True, text=True, env=env,
                                 cwd=cwd, timeout=600)
        except subprocess.TimeoutExpired:
            log("ERROR: gemini CLI timed out (600s).")
            sys.exit(1)
        except OSError as e:
            log(f"ERROR: could not run the gemini CLI at {binary}: {e}")
            sys.exit(1)
        if out.returncode != 0:
            err = (out.stderr or out.stdout or "").strip()
            if "auth method" in err.lower() or "login" in err.lower():
                log("ERROR: the gemini CLI has no login configured for subscription/OAuth use. Run "
                    "`gemini` once and choose 'Login with Google', then retry. (Or set "
                    "GEMINI_CLI_USE_API_KEY=1 to use your API key through the CLI.)")
            else:
                log(f"ERROR: gemini CLI exited {out.returncode}: {err[:500]}")
            sys.exit(1)
        try:
            d = json.loads(out.stdout)
            ans = d.get("response", "")
            stats = d.get("stats", {}).get("models", {})
            mname = next(iter(stats), "default")
            toks = stats.get(mname, {}).get("tokens", {})
        except (json.JSONDecodeError, AttributeError):
            ans, mname, toks = out.stdout.strip(), "default", {}
        return ans, mname, toks

    answer, mname, toks = _invoke(directive + "\n\n" + body)

    # If the CLI returned an agentic non-answer (it described a file action instead of returning the
    # content), retry once with a firmer directive and keep whichever reply isn't a non-answer.
    if detect_agentic_nonanswer(answer):
        log("\n--- gemini-cli: agentic non-answer detected, retrying once ---")
        firmer = ("CRITICAL: Do NOT create, edit, or reference files, and do NOT describe any "
                  "action. Output ONLY the literal requested content as your reply.")
        r_ans, r_mname, r_toks = _invoke(firmer + "\n\n" + directive + "\n\n" + body)
        if not detect_agentic_nonanswer(r_ans):
            answer, mname, toks = r_ans, r_mname, r_toks

    log("\n--- gemini-cli meta ---")
    auth = "API key (via CLI)" if use_key else "OAuth/subscription"
    log(f"backend: gemini-cli  model: {mname}  auth: {auth}  (no API rate-limit)")
    if toks:
        log(f"tokens: prompt={toks.get('prompt')} total={toks.get('total')} thoughts={toks.get('thoughts')}")

    # Code pre-flight (opt-in): syntax-check the draft; auto-retry ONCE on a syntax error.
    if preflight:
        ok, err = preflight_python(answer)
        if not ok:
            log(f"PREFLIGHT: draft failed Python syntax-check: {err} — retrying once.")
            fix = (f"Your previous output had a syntax error: {err}. "
                   "Return corrected, syntactically valid code only.")
            retry_text = directive + "\n\n" + body + "\n\n" + fix
            answer, mname2, toks2 = _invoke(retry_text)
            if toks2:
                log(f"tokens (retry): prompt={toks2.get('prompt')} total={toks2.get('total')}")
            ok2, err2 = preflight_python(answer)
            if ok2:
                log("PREFLIGHT: retry parses cleanly.")
            else:
                log(f"WARNING: PREFLIGHT still failing after one retry: {err2}. "
                    "Returning the draft anyway — SCRUTINIZE this code before trusting it.")
        else:
            log("PREFLIGHT: draft parses cleanly.")
    return answer


def main():
    # Force UTF-8 on stdout/stderr so Gemini's Unicode output (em-dashes, accents, etc.)
    # prints cleanly on Windows consoles, which often default to cp1252.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Query Gemini from the shell.")
    ap.add_argument("prompt", nargs="?", help="Prompt text. If omitted, read from stdin.")
    ap.add_argument("--backend", choices=["gemini", "gemini-cli", "fm", "ollama"], default="gemini",
                    help="gemini (cloud API, default — files/grounding/JSON) | gemini-cli (drives the "
                         "logged-in Gemini CLI on your OAuth/subscription quota — no API rate limits) | "
                         "fm (Apple on-device, free/offline/private) | ollama (local model, free/unlimited).")
    ap.add_argument("--model", default=None,
                    help="gemini: flash|pro|flash-lite|<name> (default flash). "
                         "ollama: model tag (default qwen3-coder:30b). gemini-cli: full CLI model name "
                         "or omit for the CLI's default. Ignored for fm.")
    ap.add_argument("--system", help="System instruction (role/style/constraints).")
    ap.add_argument("--file", action="append", default=[], metavar="PATH",
                    help="Attach a file (PDF/image/text/csv/...). Repeatable.")
    ap.add_argument("--search", "--grounding", dest="search", action="store_true",
                    help="Enable live Google Search grounding (for research / fresh facts).")
    ap.add_argument("--json", action="store_true", help="Force a JSON response.")
    ap.add_argument("--schema", metavar="PATH", help="Path to a JSON Schema file (implies --json).")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    ap.add_argument("--thinking-budget", type=int, default=None, dest="thinking_budget",
                    help="Token budget for model 'thinking' (0 = off, faster/cheaper on Flash).")
    ap.add_argument("--list-models", action="store_true", help="List models this key can use, then exit.")
    ap.add_argument("--preflight", action="store_true",
                    help="Treat the output as Python code: syntax-check it (no execution) and, on a "
                         "syntax error, auto-retry ONCE before returning. (gemini-cli backend.)")
    args = ap.parse_args()

    if args.list_models:
        key = api_key()
        d = http_json(f"{BASE}/v1beta/models?key={key}")
        for m in d.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"].replace("models/", ""))
        return

    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip() and not args.file:
        log("ERROR: no prompt given (pass an argument or pipe text on stdin).")
        sys.exit(2)

    # Text-only backends: on-device Apple FM, a local Ollama model, or the OAuth'd Gemini CLI.
    # File ingest and web grounding stay on the `gemini` (API) backend, where they're free.
    if args.backend in ("fm", "ollama", "gemini-cli"):
        if args.file:
            log(f"ERROR: --file is only on the gemini (API) backend (PDFs/images need it). "
                f"Use --backend gemini, or paste text into the prompt for {args.backend}.")
            sys.exit(2)
        if args.search:
            log("ERROR: --search (web grounding) is only on the gemini (API) backend.")
            sys.exit(2)
        if args.backend == "fm":
            text = call_fm(prompt, args.system, args.temperature)
        elif args.backend == "ollama":
            text = call_ollama(prompt, args.system, args.temperature, args.model, args.max_tokens)
        else:
            text = call_gemini_cli(prompt, args.system, args.temperature, args.model,
                                   preflight=args.preflight)
        print(text)
        sys.stdout.flush()
        return

    # --- gemini backend (default) ---
    key = api_key()
    model = ALIASES.get(args.model or "flash", args.model or "flash")

    parts = []
    for fp in args.file:
        if not os.path.exists(fp):
            log(f"ERROR: file not found: {fp}")
            sys.exit(2)
        parts.append(make_file_part(fp, key))
    if prompt.strip():
        parts.append({"text": prompt})

    body = {"contents": [{"role": "user", "parts": parts}]}

    if args.system:
        body["systemInstruction"] = {"parts": [{"text": args.system}]}
    if args.search:
        body["tools"] = [{"googleSearch": {}}]

    gen = {}
    if args.temperature is not None:
        gen["temperature"] = args.temperature
    if args.max_tokens is not None:
        gen["maxOutputTokens"] = args.max_tokens
    if args.thinking_budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": args.thinking_budget}
    if args.schema or args.json:
        gen["responseMimeType"] = "application/json"
    if args.schema:
        with open(args.schema) as f:
            gen["responseSchema"] = json.load(f)
    if gen:
        body["generationConfig"] = gen

    url = f"{BASE}/v1beta/models/{model}:generateContent?key={key}"
    resp = http_json(url, method="POST", body=body)

    fb = resp.get("promptFeedback", {})
    if fb.get("blockReason"):
        log(f"ERROR: Gemini blocked the prompt ({fb['blockReason']}).")
        sys.exit(1)

    cands = resp.get("candidates", [])
    if not cands:
        log(f"ERROR: empty response from Gemini.\n{json.dumps(resp)[:1500]}")
        sys.exit(1)
    cand = cands[0]
    text = extract_text(cand)
    print(text)
    sys.stdout.flush()

    # Diagnostics to stderr (Claude can read these; they don't pollute stdout).
    u = resp.get("usageMetadata", {})
    log(f"\n--- gemini meta ---")
    log(f"model: {model}")
    log(f"tokens: prompt={u.get('promptTokenCount')} output={u.get('candidatesTokenCount')} "
        f"thoughts={u.get('thoughtsTokenCount', 0)} total={u.get('totalTokenCount')}")
    fr = cand.get("finishReason")
    if fr and fr != "STOP":
        log(f"finishReason: {fr}  (output may be truncated/partial)")
    if args.search:
        srcs = grounding_sources(cand)
        if srcs:
            log("sources:")
            for title, uri in srcs:
                log(f"  - {title or '(untitled)'}: {uri}")
        else:
            log("sources: (none returned — Gemini may not have searched)")


if __name__ == "__main__":
    main()
