#!/usr/bin/env python3
"""
Minimal tool-loop agent for local Ollama models.

Gives a local model four tools (list_files, read_file, write_file, run_command)
plus finish, scoped to a sandbox working directory, and loops until the model
finishes or the turn budget runs out. Pure stdlib.

Every request/response is appended to a transcript JSONL — successful transcripts
make useful future fine-tuning data.

Usage:
    python3 agent_loop.py --model qwen3-coder:30b --workdir /path/to/sandbox \
        --prompt-file task.txt [--max-turns 25] [--num-ctx 32768] \
        [--transcript /path/to/transcript.jsonl]

Prints a one-line JSON summary to stdout at the end:
    {"finished": bool, "turns": int, "seconds": float, "stop": "finish|no_tools|max_turns|error"}
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

READ_LIMIT = 16000     # chars of a file the model may see at once
OUT_LIMIT = 6000       # chars of stdout/stderr per command
CMD_TIMEOUT = 90       # seconds per run_command
DENY = ("sudo", "rm -rf /", "shutdown", "reboot", "diskutil", "> /dev/")

SYSTEM = """You are a careful software engineering agent working inside a project directory.

Rules:
- All paths are RELATIVE to the project root. Never use absolute paths or `..`.
- Work in small steps: inspect first (list_files/read_file), then change, then VERIFY.
- After any code change, verify it by running the tests or the program itself
  (e.g. `python3 -m pytest -q` or `python3 yourscript.py ...`).
- Do not modify test files unless the task explicitly says to.
- When your verification passes, call finish with a one-line summary. Do not finish
  before you have run a successful verification.
- Provided tests may not cover every requirement. Before finishing, re-read the task
  and confirm EVERY stated requirement yourself (write and run your own checks if needed).
- If a command fails, read the error, fix the cause, and try again.
- Keep every tool call SMALL. For multi-line checks or scripts, write_file them
  (e.g. check.py) and run `python3 check.py` — never put long heredocs or multi-line
  code inside a run_command argument.
- Do not get stuck re-running commands. If you have run commands two or more times
  without writing a file in between, or you see the same error twice, STOP inspecting:
  re-read the relevant file and write_file a concrete fix. Re-running the same check
  cannot change its result — only editing the code can. Every task needs you to WRITE
  the change it asks for, not merely explore or repeatedly test.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files under a directory (relative path), recursively.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative directory, default '.'"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file (relative path).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a text file (relative path) with the given content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the project root (e.g. 'python3 -m pytest -q'). "
                       "Returns exit code, stdout and stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when the task is complete AND verified. Ends the session.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]


def _resolve(workdir: str, path: str):
    """Resolve a relative path inside the sandbox; None if it escapes."""
    full = os.path.realpath(os.path.join(workdir, path or "."))
    root = os.path.realpath(workdir)
    if full == root or full.startswith(root + os.sep):
        return full
    return None


def t_list_files(workdir, path="."):
    full = _resolve(workdir, path)
    if not full:
        return "ERROR: path escapes the project root"
    if not os.path.isdir(full):
        return f"ERROR: not a directory: {path}"
    lines, count = [], 0
    for dirpath, dirnames, filenames in os.walk(full):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git", ".pytest_cache")]
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), os.path.realpath(workdir))
            size = os.path.getsize(os.path.join(dirpath, fn))
            lines.append(f"{rel}  ({size} bytes)")
            count += 1
            if count >= 200:
                lines.append("... (truncated at 200 entries)")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(empty)"


def t_read_file(workdir, path):
    full = _resolve(workdir, path)
    if not full:
        return "ERROR: path escapes the project root"
    if not os.path.isfile(full):
        return f"ERROR: no such file: {path}"
    try:
        with open(full, "r", errors="replace") as f:
            content = f.read(READ_LIMIT + 1)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    if len(content) > READ_LIMIT:
        content = content[:READ_LIMIT] + "\n... (truncated)"
    return content


def t_write_file(workdir, path, content):
    full = _resolve(workdir, path)
    if not full:
        return "ERROR: path escapes the project root"
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return f"OK: wrote {len(content)} chars to {path}"


def t_run_command(workdir, command):
    low = command.lower()
    if any(bad in low for bad in DENY):
        return "ERROR: command blocked by policy"
    try:
        p = subprocess.run(["/bin/bash", "-c", command], cwd=workdir,
                           capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {CMD_TIMEOUT}s"
    out = (p.stdout or "")[:OUT_LIMIT]
    err = (p.stderr or "")[:OUT_LIMIT]
    return f"exit code: {p.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


# ---------------------------------------------------------------- fallback parsing
# Some models (e.g. qwen2.5-coder via Ollama) can't emit native tool_calls and
# instead write the call as JSON text. Parse those so they can still drive the loop.

TOOL_NAMES = {"list_files", "read_file", "write_file", "run_command", "finish"}
FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```", re.DOTALL)

WRITE_CONVENTION = (
    "To write a file, send {\"name\": \"write_file\", \"arguments\": {\"path\": \"...\"}} "
    "in a ```json block, then put the COMPLETE file content in a SECOND fenced code block "
    "right after it — never put multiline content inside the JSON itself."
)

NUDGE = (
    "You did not call a tool. To use a tool, respond with exactly one JSON object "
    "in a ```json code block, like:\n"
    '```json\n{"name": "run_command", "arguments": {"command": "python3 -m pytest -q"}}\n```\n'
    "Available tools: list_files(path), read_file(path), write_file(path, content), "
    "run_command(command), finish(summary). " + WRITE_CONVENTION + " If the task is fully "
    "complete AND you have run a successful verification, call finish."
)

FINISH_GATE = (
    "Not yet. Before finishing, re-read the ORIGINAL task statement. List every stated "
    "requirement and, for each one, how you verified it (provided tests may not cover them "
    "all). If any requirement is unverified, verify it now with your own checks and fix "
    "anything that fails. When every requirement is confirmed, call finish again."
)


def _coerce_call(obj):
    """Return (name, args) if obj looks like a single tool call, else None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("function"), dict):  # native-shaped {"function": {...}}
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if name in TOOL_NAMES and isinstance(args, dict):
        return name, args
    return None


def _calls_from_obj(obj):
    """Expand a parsed JSON value into zero or more (name, args) tool calls."""
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
        items = obj["tool_calls"]
    else:
        items = [obj]
    return [c for c in (_coerce_call(it) for it in items) if c]


def parse_fallback_calls(content):
    """Extract tool calls a model wrote as JSON text instead of native tool_calls.

    Supports the two-part write convention: a write_file call whose JSON omits
    (or empties) `content` takes its content from the next fenced code block in
    the same message that is not itself a tool call — multiline file bodies
    inside JSON strings are exactly what mid-size models can't escape reliably.
    """
    if not content:
        return []
    calls = []
    spare_blocks = []  # fenced blocks that are not tool-call JSON (candidate file bodies)
    for m in FENCE_RE.finditer(content):
        body = m.group(2).strip()
        found = []
        try:
            found = _calls_from_obj(json.loads(body))
        except ValueError:
            pass
        if found:
            calls.extend(found)
        elif body:
            spare_blocks.append(body)
    if not calls:
        # no fenced call: try whole content, then JSON embedded in prose
        stripped = content.strip()
        if stripped.startswith(("{", "[")):
            try:
                calls = _calls_from_obj(json.loads(stripped))
            except ValueError:
                pass
        if not calls:
            dec = json.JSONDecoder()
            idx = 0
            while True:
                i = content.find("{", idx)
                if i == -1:
                    break
                try:
                    obj, end = dec.raw_decode(content[i:])
                except ValueError:
                    idx = i + 1
                    continue
                found = _calls_from_obj(obj)
                if found:
                    calls.extend(found)
                    idx = i + end
                else:
                    idx = i + 1
    # pair content-less write_file calls with spare blocks, in order
    bi = 0
    for i, (name, args) in enumerate(calls):
        if name == "write_file" and not args.get("content") and bi < len(spare_blocks):
            calls[i] = (name, dict(args, content=spare_blocks[bi]))
            bi += 1
    return calls


MAX_GEN_TOKENS = 1600  # one tool call + a full file; caps temp-0 repetition loops.
# NOTE: a single native write_file call bigger than this gets TRUNCATED mid-JSON and
# Ollama 500s ("error parsing tool call"). Raise per-run with --max-gen-tokens.


def chat(model, messages, num_ctx):
    payload = {"model": model, "messages": messages, "tools": TOOLS, "stream": False,
               "options": {"temperature": 0, "num_ctx": num_ctx,
                           "num_predict": MAX_GEN_TOKENS}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


# ---------------------------------------------------------------- gemini backend

GEMINI_MODEL_ALIASES = {"pro": "gemini-pro-latest", "flash": "gemini-flash-latest"}


def chat_gemini(model, contents, system):
    """One generateContent call with function declarations; retries on 429/5xx."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = GEMINI_MODEL_ALIASES.get(model, model)
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": [t["function"] for t in TOOLS]}],
        "generationConfig": {"temperature": 0},
    }
    data = json.dumps(payload).encode()
    last_exc = None
    for attempt in range(5):
        req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                resp = json.load(r)
            cand = (resp.get("candidates") or [{}])[0]
            return cand.get("content", {}).get("parts", []) or []
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (429, 500, 503) and attempt < 4:
                time.sleep(8 * (2 ** attempt))  # 8, 16, 32, 64s
                continue
            raise
    raise last_exc


def chat_openai(model, messages, base_url):
    """OpenAI-compatible chat (e.g. mlx_lm server). Plain text — no tool schemas;
    the model must speak the JSON-fallback protocol from training/nudging."""
    clean = [{"role": m["role"], "content": m.get("content", "")}
             for m in messages if m.get("role") in ("system", "user", "assistant")]
    payload = {"model": model, "messages": clean, "temperature": 0,
               "max_tokens": MAX_GEN_TOKENS}
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions",
                                 json.dumps(payload).encode(),
                                 {"Content-Type": "application/json",
                                  "Authorization": "Bearer local"})
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.load(r)
    content = resp["choices"][0]["message"].get("content") or ""
    return {"role": "assistant", "content": content}


def gemini_parts_to_msg(parts):
    """Normalize Gemini reply parts to the message shape the loop understands."""
    text = "\n".join(p["text"] for p in parts
                     if "text" in p and not p.get("thought"))
    calls = [{"function": {"name": p["functionCall"].get("name", ""),
                           "arguments": p["functionCall"].get("args") or {}}}
             for p in parts if "functionCall" in p]
    return {"role": "assistant", "content": text, "tool_calls": calls}


def _ledger(rec):
    """Append one usage record to the skill's data/usage.jsonl (SMITH_LEDGER overrides).
    Progress tracking only — must never affect the run, so it swallows everything."""
    try:
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **rec}
        path = os.environ.get("SMITH_LEDGER") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "usage.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    global MAX_GEN_TOKENS
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--backend", choices=["ollama", "gemini", "openai"], default="ollama")
    ap.add_argument("--base-url", default="http://localhost:8080",
                    help="openai backend server (e.g. mlx_lm server)")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--max-gen-tokens", type=int, default=MAX_GEN_TOKENS,
                    help="per-turn generation cap (default 1600). Raise for tasks that "
                         "write large single files.")
    ap.add_argument("--finish-gate", action="store_true",
                    help="bounce the first finish call with a requirement-audit prompt "
                         "(measured null result on qwen2.5-coder:14b, 2026-07-01)")
    args = ap.parse_args()
    MAX_GEN_TOKENS = args.max_gen_tokens

    workdir = os.path.realpath(args.workdir)
    with open(args.prompt_file) as f:
        task = f.read()

    tlog = None
    if args.transcript:
        os.makedirs(os.path.dirname(args.transcript) or ".", exist_ok=True)
        tlog = open(args.transcript, "a")

    def log(kind, data):
        if tlog:
            tlog.write(json.dumps({"t": round(time.time(), 2), "kind": kind, "data": data}) + "\n")
            tlog.flush()

    backend = args.backend
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": task}]        # ollama history
    contents = [{"role": "user", "parts": [{"text": task}]}]  # gemini history
    log("task", {"model": args.model, "backend": backend, "workdir": workdir,
                 "prompt": task})

    def push_user(text):
        if backend == "gemini":
            contents.append({"role": "user", "parts": [{"text": text}]})
        else:
            messages.append({"role": "user", "content": text})

    t0 = time.time()
    finished, stop = False, "max_turns"
    turn, nudged, finish_bounced = 0, False, False
    for turn in range(1, args.max_turns + 1):
        try:
            if backend == "gemini":
                parts = chat_gemini(args.model, contents, SYSTEM)
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                msg = gemini_parts_to_msg(parts)
            elif backend == "openai":
                msg = chat_openai(args.model, messages, args.base_url)
                messages.append(msg)
            else:
                resp = chat(args.model, messages, args.num_ctx)
                msg = resp.get("message", {})
                messages.append(msg)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            stop = f"error:http-{exc.code}:{body}"
            log("error", stop)
            break
        except Exception as exc:  # noqa: BLE001
            stop = f"error:{exc}"
            log("error", stop)
            break

        log("assistant", msg)

        calls = msg.get("tool_calls") or []
        native = bool(calls)
        if not calls:
            # Fallback: some models write the call as JSON text instead.
            fb = parse_fallback_calls(msg.get("content") or "")
            if fb:
                calls = [{"function": {"name": n, "arguments": a}} for n, a in fb]
                log("fallback_parse", [{"name": n, "args": a} for n, a in fb])
        if not calls:
            if not nudged:
                # One shot at teaching the protocol before giving up.
                nudged = True
                push_user(NUDGE)
                log("nudge", NUDGE)
                continue
            # Model answered in prose with no tool call — treat as done (unverified).
            stop = "no_tools"
            break

        fb_results, tool_parts = [], []
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except ValueError:
                    raw_args = {}

            if name == "finish" and args.finish_gate and not finish_bounced:
                # First finish attempt bounces once: models tend to verify only the
                # provided tests and miss uncovered spec requirements.
                finish_bounced = True
                result = FINISH_GATE
                log("finish_gate", {"summary": raw_args.get("summary", "")})
            elif name == "finish":
                finished, stop = True, "finish"
                result = "session ended"
            elif name == "list_files":
                result = t_list_files(workdir, raw_args.get("path", "."))
            elif name == "read_file":
                result = t_read_file(workdir, raw_args.get("path", ""))
            elif name == "write_file":
                result = t_write_file(workdir, raw_args.get("path", ""),
                                      raw_args.get("content", ""))
            elif name == "run_command":
                result = t_run_command(workdir, raw_args.get("command", ""))
            else:
                result = f"ERROR: unknown tool {name}"

            if backend == "gemini" and native:
                tool_parts.append({"functionResponse":
                                   {"name": name, "response": {"result": result}}})
            elif native:
                messages.append({"role": "tool", "tool_name": name, "content": result})
            else:
                fb_results.append(f"[{name} result]\n{result}")
            log("tool", {"name": name, "args": raw_args, "result": result[:2000],
                         "native": native})

        if tool_parts:
            contents.append({"role": "user", "parts": tool_parts})
        if finished:
            break
        if not native and fb_results:
            # Non-native templates may not render the tool role; deliver results
            # as a user message instead, and restate the protocol.
            push_user("\n\n".join(fb_results) +
                      "\n\nContinue. Respond with your next single tool call as a "
                      "```json block, or call finish when done and verified. " +
                      WRITE_CONVENTION)

    summary = {"finished": finished or stop == "no_tools", "turns": turn,
               "seconds": round(time.time() - t0, 1), "stop": stop}
    log("summary", summary)
    if tlog:
        tlog.close()
    print(json.dumps(summary))
    _ledger({"script": "smith_agent", "backend": backend, "model": args.model,
             "workdir": workdir, "task": task[:120], **summary})


if __name__ == "__main__":
    main()
