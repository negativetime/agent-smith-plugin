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
        log("ERROR: GEMINI_API_KEY is not set. Ask the user to export it, then retry.")
        sys.exit(2)
    return k


# chat-generation HTTP ceiling; local thinking models (qwen3.6) can exceed 600s on
# one-shots — the gym raises this via env for scout runs
GEN_HTTP_TIMEOUT = int(os.environ.get("SMITH_GEN_TIMEOUT", "600"))


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


def call_ollama(prompt, system, temperature, model, max_tokens, images=None):
    """Local model via Ollama (http://localhost:11434). Free, unlimited, offline.
    images: optional list of base64-encoded image bytes (needs a vision model, e.g. gemma4:26b)."""
    model = model or os.environ.get("OLLAMA_MODEL") or "qwen3-coder:30b"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    user_msg = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = images
    msgs.append(user_msg)
    body = {"model": model, "messages": msgs, "stream": False}
    opts = {}
    if temperature is not None:
        opts["temperature"] = temperature
    if max_tokens is not None:
        opts["num_predict"] = max_tokens
    # Auto-size the context window: Ollama SILENTLY truncates the prompt to the server
    # default (~32k here) — long digests would lose their head without warning. Estimate
    # conservatively at 3 chars/token (markdown/code measures ~3.4; prose ~4) + output
    # headroom, round up to a power-of-two step, cap at 131072 (gpt-oss:20b native;
    # measured 2026-07-12: KV cost at 131k is negligible on MXFP4 MoE, 3/3 needle recall
    # at 52k tokens).
    est = len(prompt) // 3 + (len(system) // 3 if system else 0) + (max_tokens or 4096) + 512
    if est > 8192:
        ctx = 16384
        while ctx < est and ctx < 131072:
            ctx *= 2
        opts["num_ctx"] = min(ctx, 131072)
        if est > 131072:
            log(f"WARNING: input ~{est} est. tokens exceeds the 131072 num_ctx cap — head will truncate. Split the input or use --backend gemini (1M context).")
    if opts:
        body["options"] = opts
    req = urllib.request.Request("http://localhost:11434/api/chat", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=GEN_HTTP_TIMEOUT) as r:
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


def _subject(prompt, explicit=None, limit=160):
    """Derive a human-readable 'what was this for' line for the ledger.

    The ledger used to record only size/speed/model, so 887 runs were
    indistinguishable after the fact and nothing could be reviewed or routed on
    purpose (found 2026-07-18). An explicit --purpose wins; otherwise take the
    first substantive line of the prompt, which in practice reads like a task
    title. Set SMITH_LOG_PROMPTS=0 to record only explicit purposes.
    """
    if explicit:
        return explicit[:limit]
    if os.environ.get("SMITH_LOG_PROMPTS", "1") in ("0", "false", "no"):
        return None
    for line in (prompt or "").splitlines():
        line = " ".join(line.split())
        # Skip fences/markup-only lines that carry no meaning as a title.
        if len(line) > 12 and not line.startswith(("```", "#", "<", "-", "*", "|")):
            return line[:limit]
    return " ".join((prompt or "").split())[:limit] or None


def _ledger(rec):
    """Append one usage record to the skill's data/usage.jsonl (SMITH_LEDGER overrides).
    Progress tracking only — must never affect the run, so it swallows everything."""
    try:
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **rec}
        rec.setdefault("project", os.path.basename(os.getcwd()) or None)
        path = os.environ.get("SMITH_LEDGER") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "usage.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def call_openai_compat(prompt, system, temperature, model, max_tokens, base_url):
    """Any OpenAI-compatible /chat/completions endpoint: Groq, Cerebras, OpenRouter,
    GitHub Models, xAI — or local servers (ollama's /v1, mlx_lm server, LM Studio).
    Auth = Bearer OPENAI_API_KEY if set (local servers usually need none)."""
    BASE_ALIASES = {"groq": "https://api.groq.com/openai/v1",
                    "openrouter": "https://openrouter.ai/api/v1",
                    "openai": "https://api.openai.com/v1",
                    "ollama": "http://localhost:11434/v1"}
    base = base_url or os.environ.get("OPENAI_BASE_URL") or ""
    base = BASE_ALIASES.get(base, base).rstrip("/")
    if not base:
        log("ERROR: --backend openai needs --base-url or OPENAI_BASE_URL "
            "(shorthands: groq | openrouter | ollama — or a full .../v1 URL).")
        sys.exit(2)
    if not model:
        log("ERROR: --backend openai needs --model (endpoint-specific, e.g. "
            "openai/gpt-oss-120b on Groq).")
        sys.exit(2)
    msgs = ([{"role": "system", "content": system}] if system else [])
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    # Cloudflare-fronted APIs (e.g. Groq) 403 urllib's default UA — send a real one.
    headers = {"Content-Type": "application/json", "User-Agent": "agent-smith/1.4"}
    key = os.environ.get("OPENAI_API_KEY")
    if "groq.com" in base:
        key = os.environ.get("GROQ_API_KEY") or key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = None
    for attempt in range(3):
        req = urllib.request.Request(f"{base}/chat/completions", method="POST",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                resp = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                wait = 4 * (attempt + 1)
                log(f"HTTP {e.code} from endpoint (free tiers rate-limit); retry in {wait}s ...")
                time.sleep(wait)
                continue
            log(f"ERROR: {base} HTTP {e.code}: {msg}")
            sys.exit(1)
        except urllib.error.URLError as e:
            log(f"ERROR: can't reach {base} ({e}).")
            sys.exit(1)
    text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    u = resp.get("usage") or {}
    log("\n--- openai-compat meta ---")
    log(f"endpoint: {base}  model: {model}"
        + ("  auth: bearer" if key else "  auth: none"))
    if u:
        log(f"tokens: prompt={u.get('prompt_tokens')} output={u.get('completion_tokens')}")
    return text


def _normalize_output(text):
    """Normalize a model output for consensus comparison: strip, casefold, and
    collapse every internal whitespace run to a single space."""
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _witness_due(primary_model):
    """FSRS-style witness scheduling: trust has a forgetting curve. The re-verification
    interval for a model GROWS with its streak of consecutive witness agreements
    (interval = BASE * 2^streak runs, capped) and COLLAPSES back to BASE on any witness
    disagreement or verified-bad verdict. Verification never reaches zero; stable routes
    just earn longer intervals. Env: SMITH_WITNESS_BASE (default 4), SMITH_WITNESS_CAP
    (default 256). Setting SMITH_WITNESS_RATE switches back to legacy flat-rate sampling."""
    rate_env = os.environ.get("SMITH_WITNESS_RATE")
    if rate_env is not None:  # legacy/explicit flat-rate mode
        import random
        r = float(rate_env)
        return r > 0 and random.random() < r
    base = max(1, int(os.environ.get("SMITH_WITNESS_BASE", "4")))
    cap = int(os.environ.get("SMITH_WITNESS_CAP", "256"))
    path = os.environ.get("SMITH_LEDGER") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "usage.jsonl")
    if not os.path.isfile(path):
        return True  # no history: witness the first run
    from collections import deque
    rows = []
    with open(path) as f:
        for line in deque(f, maxlen=2000):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    streak, runs_since, seen_witness = 0, 0, False
    for r in reversed(rows):
        s = r.get("script")
        if s == "witness" and r.get("primary_model") == primary_model:
            seen_witness = True
            if r.get("agree"):
                streak += 1
            else:
                break  # disagreement: curve resets here
        elif (s == "verdict" and r.get("ref_model") == primary_model
              and r.get("verdict") == "bad"):
            break  # verified-bad: curve resets here
        elif s not in ("witness", "verdict") and r.get("model") == primary_model:
            if not seen_witness:
                runs_since += 1
    if not seen_witness:
        return True  # model never witnessed
    # NOTE: the current run's ledger row is appended BEFORE this check, so
    # runs_since already counts it — compare directly, no +1.
    return runs_since >= min(base * (2 ** streak), cap)


def _witness(prompt, system, primary_model, primary_text, images, context):
    """Drift sensor (witness verification): silently re-run a DUE local call on a
    second model and compare. Trust is continuously re-earned — see _witness_due for
    the forgetting-curve schedule. Local-only (free), fail-safe, and a disagreement
    is a SIGNAL for review logged to the ledger, never an auto-verdict (the witness
    may be the wrong one). SMITH_WITNESS_MODEL sets the witness (default gpt-oss:20b)."""
    try:
        if images or not _witness_due(primary_model):
            return
        norm = " ".join((primary_text or "").split())
        if not norm or len(norm) > 280:
            return  # only short structured outputs compare meaningfully
        wmodel = os.environ.get("SMITH_WITNESS_MODEL", "gpt-oss:20b")
        if wmodel == primary_model:
            wmodel = "gemma4:26b" if primary_model != "gemma4:26b" else "gpt-oss:20b"
        wtext = call_ollama(prompt, system, 0.0, wmodel, None)
        agree = _outputs_agree(primary_text, wtext)
        _ledger({"script": "witness", "primary_model": primary_model,
                 "witness_model": wmodel, "agree": agree, "context": context,
                 "primary_out": norm[:120],
                 "witness_out": " ".join((wtext or "").split())[:120]})
        if not agree:
            log(f"[witness] DRIFT SIGNAL: {wmodel} disagrees with {primary_model} "
                f"({context}) — review recommended; see ledger.")
    except (Exception, SystemExit):  # witness must never harm the primary run
        pass


def _strip_fences(text):
    """Drop a wrapping markdown code fence. Whether a model wraps its answer in
    ```python is a formatting habit, not a disagreement."""
    return _split_fence(text)[0]


def _split_fence(text):
    """(body, language_tag_or_None). The fence's tag is the most reliable
    language signal available, so it is captured rather than discarded."""
    t = (text or "").strip()
    m = re.match(r"^```([a-zA-Z0-9_.+-]*)[ \t]*\r?\n(.*?)```\s*$", t, re.DOTALL)
    if not m:
        return t, None
    return m.group(2).strip(), (m.group(1).strip().lower() or None)


# Fence tag -> tree-sitter grammar name. Only the languages this fleet actually
# drafts; the pack ships 306, but guessing beyond what we use buys nothing.
_TS_LANGS = {
    "swift": "swift", "js": "javascript", "javascript": "javascript",
    "jsx": "javascript", "ts": "typescript", "typescript": "typescript",
    "tsx": "tsx", "go": "go", "rust": "rust", "rs": "rust", "java": "java",
    "c": "c", "cpp": "cpp", "c++": "cpp", "objc": "objc", "kotlin": "kotlin",
    "ruby": "ruby", "rb": "ruby", "php": "php", "bash": "bash", "sh": "bash",
    "zsh": "bash", "html": "html", "css": "css", "json": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "toml", "sql": "sql",
    "dockerfile": "dockerfile", "lua": "lua", "scala": "scala",
}
# Tried in order when there is no fence tag. Deliberately short: each attempt
# is a full parse, and a wrong-language parse must be REJECTED (below), not
# silently accepted.
_TS_SNIFF = ("swift", "typescript", "javascript", "go", "rust", "json", "yaml")

# Delimiters carrying no meaning beyond what the tree shape already encodes.
# Everything else anonymous (operators, keywords) IS meaning and is kept.
_TS_PUNCT = frozenset({";", ",", "{", "}", "(", ")", "[", "]", ":", "\n"})


def _ts_skeleton(text, lang_tag=None):
    """Structural fingerprint of NON-Python source via tree-sitter, or None.

    Python has a stdlib AST and needs no dependency, so it is handled by
    _py_skeleton. Everything else previously fell through to exact-match, which
    is the bug that pinned the witness at 18% agreement — a reformatted Swift
    draft or a moved JS semicolon read as drift. The fleet drafts Swift,
    TypeScript, YAML and Dockerfiles routinely, so that fallback covered real
    traffic (found 2026-07-20).

    tree-sitter is OPTIONAL. agent-smith's zero-dependency guarantee holds: if
    the packages are absent this returns None and behaviour is exactly what it
    was. Enable with:  pip install tree-sitter tree-sitter-language-pack
    """
    if not (text or "").strip():
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except Exception:  # noqa: BLE001 — absent/broken dep must never matter
        return None

    names = ([_TS_LANGS[lang_tag]] if lang_tag and lang_tag in _TS_LANGS
             else list(_TS_SNIFF))
    src = text.encode("utf-8", "replace")
    for name in names:
        try:
            tree = get_parser(name).parse(src)
        except Exception:  # noqa: BLE001 — grammar missing for this language
            continue
        # tree-sitter is ERROR-TOLERANT: it returns a tree for nonsense input
        # rather than raising. Without this check two unrelated blobs could
        # both "parse" and compare structurally similar, so a tree carrying any
        # error is treated as not-this-language.
        if tree.root_node.has_error:
            continue
        out = []

        def walk(node):
            t = node.type
            if "comment" in t:  # prose, not structure
                return
            if node.child_count == 0:
                # Leaf: keep its text so renamed identifiers still count as drift.
                out.append(f"{t}:{node.text.decode('utf-8', 'replace')}")
                return
            out.append(t)
            # ALL children, not just named ones: operators (+ - == &&) and
            # keywords (const/let/func) are ANONYMOUS nodes in tree-sitter, so
            # walking named_children silently dropped them and made `a + b`
            # compare equal to `a - b` — a false AGREEMENT, which is the
            # dangerous direction for a drift sensor. Pure delimiters stay
            # excluded: they carry no meaning the tree shape doesn't already
            # encode, and counting `;` would resurrect the semicolon false drift.
            for ch in node.children:
                if ch.is_named:
                    walk(ch)
                elif ch.type not in _TS_PUNCT:
                    out.append(f"op:{ch.type}")

        try:
            walk(tree.root_node)
        except RecursionError:
            return None
        return f"{name}\n" + "\n".join(out)
    return None


def _py_skeleton(text):
    """Structural fingerprint of Python source, or None if it isn't parseable.

    Normalizes away the things two models legitimately differ on without either
    being wrong: formatting, comments, docstrings, and type annotations. What
    survives is the actual structure — names, control flow, operations.
    """
    try:
        import ast
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    try:
        import ast
        for node in ast.walk(tree):
            # Type hints are style, not behavior.
            if isinstance(node, ast.arg):
                node.annotation = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.returns = None
            if isinstance(node, ast.AnnAssign):
                node.annotation = ast.Name(id="_", ctx=ast.Load())
            # Docstrings are prose; two correct answers word them differently.
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)) and getattr(node, "body", None):
                first = node.body[0]
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str) and len(node.body) > 1):
                    node.body = node.body[1:]
        return ast.dump(tree, annotate_fields=True, include_attributes=False)
    except Exception:  # noqa: BLE001 — a sensor must never raise
        return None


def _outputs_agree(a, b):
    """True when two model outputs agree.

    Exact-match after normalization is right for the SHORT structured outputs
    this was built for (classify/extract). It is WRONG for code: it was applied
    to one-shot code generation and reported drift for markdown fences and type
    hints, so the sensor sat at 18% agreement, every model's trust streak stayed
    pinned at 0, and the FSRS interval never grew (found 2026-07-18). When both
    sides are parseable Python, compare structure instead.
    """
    a_s, a_lang = _split_fence(a)
    b_s, b_lang = _split_fence(b)
    if _normalize_output(a_s) == _normalize_output(b_s):
        return True
    ska, skb = _py_skeleton(a_s), _py_skeleton(b_s)
    if ska is not None and skb is not None:
        return ska == skb
    # Non-Python code: same structural comparison via tree-sitter when it is
    # installed. Falls through to the old exact-match result when it is not, so
    # the zero-dependency path is unchanged.
    tsa = _ts_skeleton(a_s, a_lang or b_lang)
    tsb = _ts_skeleton(b_s, b_lang or a_lang)
    if tsa is not None and tsb is not None:
        return tsa == tsb
    return False


def run_batch(args, prompt):
    """--batch MANIFEST: run one prompt over many files on the LOCAL backend, writing one
    output file per item — Claude stays out of the per-item loop entirely (zero tokens/item).
    Manifest = one input path per line (# comments / blank lines skipped). Images
    (png/jpg/jpeg/gif/webp) attach as vision input (auto-picks gemma4:26b); other files are
    read as text (first 24K chars) and appended to the prompt. Per-item results land in
    --out-dir as <name>.out.txt; ONE JSON summary prints to stdout.

    --consensus MODEL2 additionally runs every item on MODEL2 (temperature forced to 0 for
    BOTH models unless the user passed --temperature). Agreement (normalized-equal outputs)
    writes <name>.out.txt as usual; disagreement writes <name>.A.txt (primary) +
    <name>.B.txt (MODEL2) and queues the item's input path in <out-dir>/_escalate.txt.
    An escalated item is a SUCCESSFUL item: ok = agreed + escalated, and the ledger status
    stays "ok" unless the failed list is non-empty. A backend error from EITHER model puts
    the item on the failed list instead of the escalation queue."""
    IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    if args.backend != "ollama":
        log("ERROR: --batch runs on --backend ollama only (free/unlimited is the point; "
            "for a handful of metered API calls, loop gemini.py directly).")
        sys.exit(2)
    if args.file:
        log("ERROR: --batch and --file don't combine — list the inputs in the manifest.")
        sys.exit(2)
    if not prompt.strip():
        log("ERROR: --batch needs a prompt (the operation to run on each item).")
        sys.exit(2)
    if args.consensus is not None and not args.consensus.strip():
        log("ERROR: --consensus needs a non-empty MODEL2.")
        sys.exit(2)
    t0 = time.time()
    try:
        with open(args.batch) as f:
            items = [ln.strip() for ln in f
                     if ln.strip() and not ln.strip().startswith("#")]
    except OSError as e:
        log(f"ERROR: can't read manifest {args.batch}: {e}")
        sys.exit(2)
    if not items:
        log("ERROR: manifest is empty.")
        sys.exit(2)
    any_img = any(p.lower().endswith(IMG_EXTS) for p in items)
    model = args.model or ("gemma4:26b" if any_img else "qwen3-coder:30b")
    consensus = args.consensus
    temperature = args.temperature
    if consensus:
        if consensus == model:
            log(f"ERROR: --consensus MODEL2 equals the effective primary model ({model}) — "
                "two votes from one model is no consensus. Pick a different model.")
            sys.exit(2)
        if temperature is None:
            temperature = 0.0  # determinism: exact-match voting wants repeatable outputs
        if any_img:
            log(f"note: manifest contains images — MODEL2 ({consensus}) must be a vision "
                "model, or its votes will be silent garbage.")
    out_dir = args.out_dir or (os.path.splitext(args.batch)[0] + "_out")
    os.makedirs(out_dir, exist_ok=True)
    escalate_path = os.path.join(out_dir, "_escalate.txt")
    if consensus:
        # The escalation queue describes THIS run only — truncate any previous run's.
        open(escalate_path, "w").close()
    ok, failed, consec_exit = 0, [], 0
    agreed, escalated = 0, 0
    for i, path in enumerate(items, 1):
        base = os.path.basename(path)
        log(f"[{i}/{len(items)}] {base} ...")
        try:
            if not os.path.exists(path):
                raise RuntimeError("file not found")
            images, item_prompt = None, prompt
            if path.lower().endswith(IMG_EXTS):
                if os.path.getsize(path) > 20_000_000:
                    raise RuntimeError("image too large (>20MB)")
                with open(path, "rb") as fh:
                    images = [base64.b64encode(fh.read()).decode()]
            else:
                with open(path, "r", errors="replace") as fh:
                    item_prompt = f"{prompt}\n\n--- {base} ---\n{fh.read(24_000)}"
            text = call_ollama(item_prompt, args.system, temperature, model,
                               args.max_tokens, images)
            out_path = os.path.join(out_dir, base + ".out.txt")
            if consensus:
                text2 = call_ollama(item_prompt, args.system, temperature, consensus,
                                    args.max_tokens, images)
                a_path = os.path.join(out_dir, base + ".A.txt")
                b_path = os.path.join(out_dir, base + ".B.txt")
                if _outputs_agree(text, text2):
                    with open(out_path, "w") as fh:
                        fh.write(text)
                    for stale in (a_path, b_path):  # re-run: item disagreed last time
                        if os.path.exists(stale):
                            os.remove(stale)
                    agreed += 1
                else:
                    with open(a_path, "w") as fh:
                        fh.write(text)
                    with open(b_path, "w") as fh:
                        fh.write(text2)
                    if os.path.exists(out_path):  # re-run: item agreed last time
                        os.remove(out_path)
                    with open(escalate_path, "a") as fh:
                        fh.write(path + "\n")
                    escalated += 1
                    log(f"  DISAGREE: escalated ({base}.A.txt vs {base}.B.txt)")
            else:
                with open(out_path, "w") as fh:
                    fh.write(text)
                _witness(item_prompt, args.system, model, text, images, f"batch:{base}")
            ok += 1  # consensus mode: ok = agreed + escalated (both calls succeeded)
            consec_exit = 0
        except SystemExit:
            # call_ollama exits on server/model errors; count as an item failure, but
            # three in a row means the backend is down — stop wasting the queue.
            # With --consensus this counts AT MOST ONCE PER ITEM (whichever of the two
            # calls exited) and resets only when BOTH calls succeed.
            failed.append(base)
            consec_exit += 1
            if consec_exit >= 3:
                log("ABORT: 3 consecutive backend failures — is `ollama serve` up?")
                break
        except Exception as exc:  # noqa: BLE001 — item isolation is the contract
            failed.append(base)
            consec_exit = 0
            log(f"  FAILED: {exc}")
    summary = {"batch": len(items), "ok": ok, "failed": failed[:20],
               "failed_count": len(failed), "out_dir": out_dir, "model": model,
               "seconds": round(time.time() - t0, 1)}
    rec = {"script": "gemini", "backend": "ollama", "model": model,
           "purpose": _subject(prompt, args.purpose), "tag": args.tag,
           "batch": len(items), "ok": ok, "failed_count": len(failed),
           "images": sum(1 for p in items if p.lower().endswith(IMG_EXTS)) or None,
           "seconds": summary["seconds"],
           # escalations are successes awaiting review — only real failures flip status
           "status": "ok" if not failed else "partial"}
    if consensus:
        summary["consensus_model"] = consensus
        summary["agreed"] = agreed
        summary["escalated"] = escalated
        rec.update({"consensus_model": consensus, "agreed": agreed,
                    "escalated": escalated})
    print(json.dumps(summary))
    _ledger(rec)


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
    ap.add_argument("--backend", choices=["gemini", "gemini-cli", "fm", "ollama", "openai"],
                    default="gemini",
                    help="gemini (cloud API, default — files/grounding/JSON) | gemini-cli (drives the "
                         "logged-in Gemini CLI on your OAuth/subscription quota — no API rate limits) | "
                         "fm (Apple on-device, free/offline/private) | ollama (local model, free/unlimited) | "
                         "openai (ANY OpenAI-compatible endpoint via --base-url: Groq/Cerebras/OpenRouter/"
                         "GitHub Models/local servers; auth = OPENAI_API_KEY env if set).")
    ap.add_argument("--base-url", default=None, dest="base_url",
                    help="openai backend endpoint base (or OPENAI_BASE_URL env), "
                         "e.g. https://api.groq.com/openai/v1 or http://localhost:11434/v1")
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
    ap.add_argument("--batch", metavar="MANIFEST",
                    help="Batch mode (ollama backend only): file listing one input path per "
                         "line; runs the prompt over every item, writes <name>.out.txt each "
                         "to --out-dir, prints ONE JSON summary. Zero Claude tokens per item.")
    ap.add_argument("--out-dir", default=None, dest="out_dir",
                    help="--batch output directory (default: <manifest>_out).")
    ap.add_argument("--consensus", default=None, metavar="MODEL2",
                    help="Consensus batch mode (requires --batch, ollama backend only): run "
                         "every item on BOTH the primary model and MODEL2 (temperature 0 "
                         "unless --temperature). Agreement writes <name>.out.txt as normal; "
                         "disagreement writes <name>.A.txt/<name>.B.txt and appends the item "
                         "to <out-dir>/_escalate.txt (re-run those on a stronger model, or "
                         "review by hand). Designed for SHORT structured outputs "
                         "(classify/extract) where exact-match voting is meaningful. MODEL2 "
                         "must differ from the primary, and must be a vision model when the "
                         "manifest contains images.")
    ap.add_argument("--purpose", default=None, metavar="TEXT",
                    help="One-line 'what this run is for', recorded in the usage ledger. "
                         "Auto-derived from the prompt when omitted.")
    ap.add_argument("--tag", default=None, metavar="TASKSHAPE",
                    help="Task-shape label (research | doc-format | classify | vision ...). "
                         "Groups the ledger and feeds hebbian routing weights.")
    ap.add_argument("--preflight", action="store_true",
                    help="Treat the output as Python code: syntax-check it (no execution) and, on a "
                         "syntax error, auto-retry ONCE before returning. (gemini-cli backend.)")
    args = ap.parse_args()
    t0 = time.time()

    if args.consensus and (not args.batch or args.backend != "ollama"):
        log("ERROR: --consensus MODEL2 only works with --batch on --backend ollama "
            "(two local models vote per item; disagreement fires escalation).")
        sys.exit(2)

    if args.list_models:
        key = api_key()
        d = http_json(f"{BASE}/v1beta/models?key={key}")
        for m in d.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"].replace("models/", ""))
        return

    # Piped stdin is INPUT DATA: with a prompt arg it appends below the prompt (the
    # documented `cat spec.txt | gemini.py "Make a checklist"` shape); with no prompt
    # arg it IS the prompt. Previously stdin was silently dropped when a prompt arg
    # was present — every backend lost the piped payload (caught 2026-07-12).
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    if args.prompt is not None:
        prompt = f"{args.prompt}\n\n{stdin_text}" if stdin_text.strip() else args.prompt
    else:
        prompt = stdin_text
    if not prompt.strip() and not args.file:
        log("ERROR: no prompt given (pass an argument or pipe text on stdin).")
        sys.exit(2)

    if args.batch:
        run_batch(args, prompt)
        return

    # Text-only backends: on-device Apple FM, a local Ollama model, or the OAuth'd Gemini CLI.
    # File ingest and web grounding stay on the `gemini` (API) backend, where they're free.
    if args.backend in ("fm", "ollama", "gemini-cli", "openai"):
        images = []
        if args.file:
            # Local vision: IMAGE files ride the ollama backend (gemma4:26b has native
            # vision) — free/unlimited/private. Non-image files still need the API.
            IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
            bad = [f for f in args.file if not f.lower().endswith(IMG_EXTS)]
            if args.backend != "ollama" or bad:
                log(f"ERROR: --file on local backends: only IMAGE files "
                    f"(png/jpg/jpeg/gif/webp) and only on --backend ollama (local vision "
                    f"via gemma4:26b). PDFs/docs/other need --backend gemini.")
                sys.exit(2)
            for fp in args.file:
                if not os.path.exists(fp):
                    log(f"ERROR: file not found: {fp}")
                    sys.exit(2)
                if os.path.getsize(fp) > 20_000_000:
                    log(f"ERROR: image too large (>20MB): {fp}")
                    sys.exit(2)
                with open(fp, "rb") as fh:
                    images.append(base64.b64encode(fh.read()).decode())
        if args.search:
            log("ERROR: --search (web grounding) is only on the gemini (API) backend.")
            sys.exit(2)
        if args.backend == "fm":
            model_eff = "fm"
            text = call_fm(prompt, args.system, args.temperature)
        elif args.backend == "ollama":
            # vision needs a vision model: auto-pick gemma4 when images are present
            model_eff = args.model or ("gemma4:26b" if images else "qwen3-coder:30b")
            text = call_ollama(prompt, args.system, args.temperature, model_eff,
                               args.max_tokens, images)
        elif args.backend == "openai":
            model_eff = args.model or "unset"
            text = call_openai_compat(prompt, args.system, args.temperature, args.model,
                                      args.max_tokens, args.base_url)
        else:
            model_eff = args.model or "default"
            text = call_gemini_cli(prompt, args.system, args.temperature, args.model,
                                   preflight=args.preflight)
        print(text)
        sys.stdout.flush()
        _ledger({"script": "gemini", "backend": args.backend, "model": model_eff,
                 "purpose": _subject(prompt, args.purpose), "tag": args.tag,
                 "files": [os.path.basename(f) for f in args.file] or None,
                 "prompt_chars": len(prompt), "out_chars": len(text),
                 "images": len(images) or None,
                 "seconds": round(time.time() - t0, 1), "status": "ok"})
        if args.backend == "ollama":
            _witness(prompt, args.system, model_eff, text, images, "oneshot")
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
    _ledger({"script": "gemini", "backend": "gemini", "model": model,
             "purpose": _subject(prompt, args.purpose), "tag": args.tag,
             "file_names": [os.path.basename(f) for f in args.file] or None,
             "prompt_chars": len(prompt), "out_chars": len(text),
             "tokens_prompt": u.get("promptTokenCount"),
             "tokens_out": u.get("candidatesTokenCount"),
             "search": bool(args.search), "files": len(args.file),
             "seconds": round(time.time() - t0, 1), "status": "ok"})
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
