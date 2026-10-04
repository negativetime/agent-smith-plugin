#!/usr/bin/env python3
"""
Minimal tool-loop agent for local Ollama models.

Gives a local model four tools (list_files, read_file, write_file, run_command)
plus finish, scoped to a sandbox working directory, and loops until the model
finishes or the turn budget runs out. Pure stdlib.

Every request/response is appended to a transcript JSONL — successful transcripts
are future fine-tuning data (see PROGRAM.md §5).

Usage:
    python3 agent_loop.py --model gpt-oss:20b --workdir /path/to/sandbox \
        --prompt-file task.txt [--max-turns 25] [--num-ctx 32768] \
        [--transcript /path/to/transcript.jsonl]

Prints a one-line JSON summary to stdout at the end:
    {"finished": bool, "turns": int, "seconds": float,
     "stop": "finish|no_tools|finish_no_write|no_tools_no_write|max_turns|error"}
`finished` is only true if a write_file actually succeeded this session — a
finish/no_tools stop with zero writes is downgraded to *_no_write and NOT counted
as finished (models can talk themselves into "done" without changing anything).

Explore mode (read-only, the fleet's stand-in for a Claude Explore subagent):
    python3 smith_agent.py --explore /path/to/repo --prompt-file question.txt
    python3 smith_agent.py --explore /path/to/repo --fanout questions.txt [-j 4]
The model gets list_files / read_file (line-numbered) / grep / finish(report) and NO
write or shell tool. `finished` means a non-empty report came back; its file:line
citations are checked against the real files (`cites`). --fanout runs one explore
agent per question in parallel subprocesses and writes one combined fanout.md.
Defaults to the flat-rate z.ai GLM Coding Plan when --model/--backend are omitted.
"""
import argparse
import concurrent.futures
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import uuid
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

READ_LIMIT = 16000     # chars of a file the model may see at once
OUT_LIMIT = 6000       # chars of stdout/stderr per command
CMD_TIMEOUT = 90       # seconds per run_command
DENY = ("sudo", "rm -rf /", "shutdown", "reboot", "diskutil", "> /dev/")

# Repo mode only: commands that reach OUTSIDE the worktree, or publish. Regex with word
# boundaries rather than DENY substrings, because "gh " is a substring of "through ".
# Gated to repo mode so the gym's sandbox baselines keep their measured behaviour.
DENY_RE = (
    r"\bgit\s+push\b",
    r"\bgit\s+remote\b",
    r"\bgit\s+config\s+--global\b",
    r"(?:^|[|;&(]\s*)gh\s",
)

# Repo-mode state. WRITE_SCOPE gates write_file ADVISORY-only: run_command hands the model
# a shell, so the authoritative gate is the settlement diff (see settle_repo). READ_SCOPE
# only filters list_files and is likewise not a boundary.
WRITE_SCOPE = None
READ_SCOPE = None
REPO_MODE = False
SCOPE_SRC = {}   # raw glob strings, for error messages

# Explore mode: read-only. The tool list sent to the model has no write/shell tool, and the
# dispatcher refuses them anyway (the JSON-fallback parser still recognises their names).
EXPLORE = False
GREP_MAX = 80          # matches shown per grep call
LIST_MAX = 300         # entries shown per list_files call in explore mode
INDEX_MAX = 60000      # files indexed per explore run
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".build", "build",
             "dist", "DerivedData", ".venv", "venv", "Pods", ".worktrees", ".dd"}

# z.ai's flat-rate GLM Coding Plan, the same route gemini.py calls `--base-url zai-coding`.
# Its completions path has no /v1 segment, and it rate-limits CONCURRENCY (~6 in flight).
ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_MAX_JOBS = 5       # headroom under the ~6 ceiling for another session's z.ai call


def glob_to_re(glob: str):
    """Translate a scope glob to a regex. `**` spans separators, `*`/`?` do not."""
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i + 1:i + 2] == "*":
                out.append(".*")
                i += 2
                if glob[i:i + 1] == "/":   # `a/**/b` should also match `a/b`
                    out.append("(?:|(?<=/))")
                    i += 1
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def in_scope(path: str, scope) -> bool:
    """True if `path` (repo-relative, forward slashes) matches any glob in `scope`."""
    return any(rx.match(path) for rx in scope or ())

# The MINIMAL-CODE block is adapted from Ponytail (github.com/DietrichGebert/ponytail),
# A/B-validated in agent-gym 2026-07-14: pass rate held/improved on all 3 fleet models,
# -17%/-26% code on 30b/gemma4, smith turns halved (runs/20260714-135025 vs -144501).
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

MINIMAL-CODE DISCIPLINE:
Think like a lazy senior developer — the best code is the code you never wrote.
Before writing any code, stop at the first rung that applies:
1. Is it actually needed for a stated requirement? If not, do not build it.
2. Does code you can SEE in this project already solve it? Reuse it — but never
   assume a helper exists without reading it first.
3. Can the standard library do it? Prefer stdlib over hand-rolled code, and never
   add a dependency the task did not ask for.
4. Can it be a few plain lines instead of a class or framework? Write the smallest
   thing that works.
Hard rules: no unrequested abstractions, wrapper classes, config options, or extra
files; boring and direct beats clever; deletion beats addition.
Minimal NEVER means incomplete: every stated requirement must still be implemented
and verified. Cut ceremony, not requirements.
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

SYSTEM_EXPLORE = """You are a careful code investigator working READ-ONLY inside a project directory.
Your job is to answer the user's question about this code, with evidence.

Tools: list_files, grep, read_file, finish. You cannot edit files or run commands.

Rules:
- All paths are RELATIVE to the project root. Never use absolute paths or `..`.
- Search first, then read. Use grep to find where something lives, then read_file with
  start_line/end_line around the hits. Do not read whole large files when a range will do.
- read_file shows line numbers. Every factual claim in your answer must cite
  `path:line` (or `path:start-end`) and quote the key line, so it can be checked.
- Never guess. If you cannot find something, say "not found" and list what you searched.
  A wrong answer is far worse than an honest "not found".
- Do not repeat a search or read you have already done. Each tool call should learn
  something new. Stop investigating once you can answer.
- When done, call finish with `report`: your complete answer in markdown. Lead with the
  direct answer, then the evidence (citations + quoted lines), then anything uncertain.
"""

EXPLORE_TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files under a directory (relative path), recursively. "
                       "Optional glob filters by name or path, e.g. '*.swift'.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative directory, default '.'"},
            "glob": {"type": "string", "description": "Optional filter, e.g. '*.py'"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "Search file contents with a regular expression. Returns "
                       "path:line: text for each match.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "Relative directory or file, default '.'"},
            "glob": {"type": "string", "description": "Optional file filter, e.g. '*.swift'"},
            "ignore_case": {"type": "boolean"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file (relative path) with line numbers. Pass "
                       "start_line/end_line to read a range.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when you can answer. Ends the session.",
        "parameters": {"type": "object", "properties": {
            "report": {"type": "string",
                       "description": "Complete markdown answer with path:line citations"}},
            "required": ["report"]}}},
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
            if READ_SCOPE is not None and not in_scope(rel.replace(os.sep, "/"), READ_SCOPE):
                continue
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
    rel = os.path.relpath(full, os.path.realpath(workdir)).replace(os.sep, "/")
    if rel == ".git" or rel.startswith(".git/"):
        # In a LINKED worktree `.git` is a FILE holding the gitdir pointer; overwriting it
        # breaks the worktree in a way that reads as a git bug.
        return "ERROR: writing to .git is not allowed"
    if WRITE_SCOPE is not None and not in_scope(rel, WRITE_SCOPE):
        return ("ERROR: path is outside the task's write scope: " + rel +
                "\nAllowed: " + ", ".join(SCOPE_SRC.get("write", [])) +
                "\nChange a file inside the scope instead.")
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return f"OK: wrote {len(content)} chars to {path}"


def t_run_command(workdir, command):
    low = command.lower()
    if any(bad in low for bad in DENY):
        return "ERROR: command blocked by policy"
    if REPO_MODE and any(re.search(p, low) for p in DENY_RE):
        return ("ERROR: command blocked in repo mode (it would reach outside the worktree "
                "or publish). Read-only git is fine.")
    try:
        p = subprocess.run(["/bin/bash", "-c", command], cwd=workdir,
                           capture_output=True, text=True, timeout=CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {CMD_TIMEOUT}s"
    out = (p.stdout or "")[:OUT_LIMIT]
    err = (p.stderr or "")[:OUT_LIMIT]
    return f"exit code: {p.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


# ---------------------------------------------------------------- explore tools

_INDEX = {}


def explore_index(root):
    """Repo-relative paths of the files an explore run may see, built once per run.

    In a git checkout this is `git ls-files` (tracked + untracked-not-ignored), so build
    output and vendored trees stay out. Elsewhere a walk that skips SKIP_DIRS. Every entry
    is re-checked with isfile: the git index still lists files deleted from the tree."""
    if root in _INDEX:
        return _INDEX[root]
    files = None
    try:
        p = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, timeout=60)
        if p.returncode == 0:
            files = sorted({f for f in p.stdout.decode(errors="replace").split("\0") if f})
    except (OSError, subprocess.TimeoutExpired):
        pass
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                files.append(os.path.relpath(os.path.join(dirpath, fn), root)
                             .replace(os.sep, "/"))
            if len(files) > INDEX_MAX:
                break
    files = [f for f in files[:INDEX_MAX] if os.path.isfile(os.path.join(root, f))]
    _INDEX[root] = files
    return files


def _under(rel, path):
    """True if repo-relative file `rel` is `path` itself or inside directory `path`."""
    path = (path or ".").strip().strip("/")
    if path in ("", "."):
        return True
    return rel == path or rel.startswith(path + "/")


def _glob_ok(rel, glob):
    return not glob or fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)


def _scoped_files(root, path, glob):
    """Files under `path` matching `glob`, or an error string if `path` escapes/is missing."""
    full = _resolve(root, path or ".")
    if not full:
        return "ERROR: path escapes the project root"
    if not os.path.exists(full):
        return f"ERROR: no such file or directory: {path}"
    rel = os.path.relpath(full, os.path.realpath(root)).replace(os.sep, "/")
    return [f for f in explore_index(root) if _under(f, rel) and _glob_ok(f, glob)]


def t_x_list(root, path=".", glob=None):
    files = _scoped_files(root, path, glob)
    if isinstance(files, str):
        return files
    if not files:
        return "(no files)"
    lines = []
    for f in files[:LIST_MAX]:
        try:
            lines.append(f"{f}  ({os.path.getsize(os.path.join(root, f))} bytes)")
        except OSError:
            lines.append(f)
    if len(files) > LIST_MAX:
        lines.append(f"... ({len(files) - LIST_MAX} more; narrow path or glob)")
    return "\n".join(lines)


def t_x_read(root, path, start_line=None, end_line=None):
    """Line-numbered read, so the model can cite path:line. Truncation names the next line."""
    full = _resolve(root, path)
    if not full:
        return "ERROR: path escapes the project root"
    if not os.path.isfile(full):
        return f"ERROR: no such file: {path}"
    try:
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else None
    except (TypeError, ValueError):
        return "ERROR: start_line/end_line must be integers"
    out, size, last = [], 0, 0
    try:
        with open(full, "r", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if n < start:
                    continue
                if end is not None and n > end:
                    break
                row = f"{n:6}| {line.rstrip()[:400]}"
                if size + len(row) > READ_LIMIT and out:
                    out.append(f"... (truncated; call read_file with start_line={n} to continue)")
                    return "\n".join(out)
                out.append(row)
                size += len(row) + 1
                last = n
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    if not out:
        return f"(no lines in range; the file has {last or 'fewer than ' + str(start)} lines)"
    return "\n".join(out)


def t_x_grep(root, pattern, path=".", glob=None, ignore_case=False):
    if not pattern:
        return "ERROR: pattern is required"
    note = ""
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        rx = re.compile(re.escape(pattern), re.IGNORECASE if ignore_case else 0)
        note = f"(invalid regex: {exc}; searched it as literal text)\n"
    files = _scoped_files(root, path, glob)
    if isinstance(files, str):
        return files
    hits, total = [], 0
    for rel in files:
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > 2_000_000:
                continue
            with open(full, "rb") as fb:
                if b"\0" in fb.read(1024):
                    continue
            with open(full, "r", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line):
                        total += 1
                        if len(hits) < GREP_MAX:
                            hits.append(f"{rel}:{n}: {line.strip()[:240]}")
        except OSError:
            continue
    if not hits:
        return note + f"(no matches in {len(files)} files)"
    more = (f"\n... ({total - GREP_MAX} more matches not shown; narrow the pattern, path "
            f"or glob)") if total > GREP_MAX else ""
    return note + "\n".join(hits) + more


CITE_RE = re.compile(r"([A-Za-z0-9_.@+-][A-Za-z0-9_./@+-]*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")


def check_citations(root, report):
    """Count path:line citations that point at a real file and a line it actually has.

    Cheap, mechanical, and aimed at the fleet's main failure: a confident answer about a
    file or line that does not exist. It cannot check that the quoted text is right."""
    valid, invalid, counts = 0, [], {}
    for m in CITE_RE.finditer(report or ""):
        rel, a, b = m.group(1), int(m.group(2)), m.group(3)
        rel = rel[2:] if rel.startswith("./") else rel
        full = _resolve(root, rel)
        if full and not os.path.isfile(full):
            # Allow a bare basename when exactly one indexed file has it.
            same = [f for f in explore_index(root) if f.rsplit("/", 1)[-1] == rel]
            full = os.path.join(root, same[0]) if len(same) == 1 else None
        n = None
        if full and os.path.isfile(full):
            if full not in counts:
                try:
                    with open(full, "rb") as f:
                        counts[full] = sum(1 for _ in f)
                except OSError:
                    counts[full] = 0
            n = counts[full]
        if n and 1 <= a <= n and (not b or int(b) <= n):
            valid += 1
        else:
            invalid.append(m.group(0))
    return {"valid": valid, "invalid": len(invalid), "invalid_examples": invalid[:5]}


# ---------------------------------------------------------------- fallback parsing
# Some models (e.g. qwen2.5-coder via Ollama) can't emit native tool_calls and
# instead write the call as JSON text. Parse those so they can still drive the loop.

TOOL_NAMES = {"list_files", "read_file", "write_file", "run_command", "grep", "finish"}
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

NUDGE_EXPLORE = (
    "You did not call a tool. To use a tool, respond with exactly one JSON object "
    "in a ```json code block, like:\n"
    '```json\n{"name": "grep", "arguments": {"pattern": "def main"}}\n```\n'
    "Available tools: list_files(path, glob), grep(pattern, path, glob, ignore_case), "
    "read_file(path, start_line, end_line), finish(report). When you can answer, call "
    "finish with your complete markdown report, citing path:line for every claim."
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

# Ollama `think` field, set from --think (None = the model's default). This loop runs at
# temperature 0, which is exactly where deepseek-v4.1-flash's thinking loops forever.
THINK = None


def chat(model, messages, num_ctx, tools=True):
    payload = {"model": model, "messages": messages, "stream": False,
               "options": {"temperature": 0, "num_ctx": num_ctx,
                           "num_predict": MAX_GEN_TOKENS}}
    if THINK is not None:
        payload["think"] = THINK
    if tools:
        payload["tools"] = TOOLS
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


def chat_openai(model, messages, base_url, tools=True, api_key=None,
                completions_path="/v1/chat/completions"):
    """OpenAI-compatible chat (e.g. mlx_lm server, LM Studio, or an authenticated
    cloud host like z.ai). Sends native tool schemas like the ollama backend when
    `tools`; the JSON-fallback protocol (parse_fallback_calls/NUDGE) still applies
    underneath if the model ignores them and answers in prose instead (e.g.
    gpt-oss's own channel format).

    api_key defaults to the literal string "local" (unauthenticated local servers
    ignore it) — pass a real key for a cloud host. completions_path overrides the
    default "/v1/chat/completions" suffix; not every OpenAI-compatible host follows
    that convention (found 2026-07-28: z.ai's Coding Plan endpoint is base_url +
    "/chat/completions", no "/v1" segment — the hardcoded default 404s there)."""
    clean = []
    for m in messages:
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        item = {"role": role, "content": m.get("content", "") or ""}
        if role == "assistant" and m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        if role == "tool" and m.get("tool_call_id"):
            item["tool_call_id"] = m["tool_call_id"]
        clean.append(item)
    payload = {"model": model, "messages": clean, "temperature": 0,
               "max_tokens": MAX_GEN_TOKENS}
    if tools:
        payload["tools"] = TOOLS
    data = json.dumps(payload).encode()
    for attempt in range(5):
        req = urllib.request.Request(base_url.rstrip("/") + completions_path, data,
                                     {"Content-Type": "application/json",
                                      "Authorization": f"Bearer {api_key or 'local'}"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as exc:
            # z.ai answers a 7th concurrent request with an instant 429 (code 1302), which a
            # parallel fan-out WILL hit. Same request again after a backoff; never a reroute.
            if exc.code in (429, 502, 503) and attempt < 4:
                time.sleep(4 * (2 ** attempt))  # 4, 8, 16, 32s
                continue
            raise
    msg = resp["choices"][0]["message"]
    return {"role": "assistant", "content": msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or []}


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


# ---------------------------------------------------------------- repo mode

# Build artifacts the VERIFY command itself creates (the task tells the model to run
# pytest, which writes __pycache__). These are never deliverables, so they must not fail an
# otherwise clean run — but they are unstaged and REPORTED as `artifacts`, never hidden.
# A repo with a complete .gitignore never gets here; this exists because most do not.
ARTIFACT_GLOBS = ("**/__pycache__/**", "**/*.pyc", "**/.pytest_cache/**", "**/.DS_Store")


class RepoError(Exception):
    """Preflight or settlement refusal. Raised BEFORE the model runs wherever possible."""


def git(cwd, *args, binary=False):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                       text=not binary)
    if p.returncode != 0:
        err = p.stderr if not binary else p.stderr.decode(errors="replace")
        raise RepoError(f"git {' '.join(args)}: {err.strip()}")
    return p.stdout if binary else p.stdout.strip()


FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
CONTRACT_KEYS = {"model", "backend", "tag", "base_url", "api_key_env",
                 "completions_path", "think", "verify", "write_scope", "read_scope",
                 "allow_delete", "max_turns", "max_gen_tokens", "num_ctx"}


def parse_contract(path):
    """Parse a task contract: `---` front matter then the task prose.

    Values are JSON where it parses (lists, bools, numbers) and a bare string otherwise,
    so `write_scope: ["a/**"]` and `model: glm-4.6` both work without a YAML dependency.
    """
    raw = open(path).read()
    m = FM_RE.match(raw)
    if not m:
        raise RepoError(f"{path}: contract must open with a --- front-matter block")
    meta = {}
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ":" not in ln:
            raise RepoError(f"{path}: contract line is not `key: value`: {ln}")
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if k not in CONTRACT_KEYS:
            raise RepoError(f"{path}: unknown contract key {k!r} "
                            f"(known: {', '.join(sorted(CONTRACT_KEYS))})")
        try:
            meta[k] = json.loads(v)
        except ValueError:
            meta[k] = v.strip("'\"")
    body = raw[m.end():].strip()
    if not body:
        raise RepoError(f"{path}: contract has no task body after the front matter")
    return meta, body


# An unbounded scope is the same as no scope, so it is refused rather than silently
# passing everything. `.git` is refused because writing there breaks the worktree.
BAD_SCOPE = {"**", "*", "**/*", ".", "./**", "/", ""}


def validate_scope(globs, kind):
    if not globs:
        raise RepoError(f"repo mode requires --{kind}-scope (an unscoped run has no gate)")
    out = []
    for g in globs:
        g = g.strip()
        if g in BAD_SCOPE:
            raise RepoError(f"--{kind}-scope {g!r} matches the whole repo; scope it down")
        if g.startswith("/") or g.startswith("~"):
            raise RepoError(f"--{kind}-scope {g!r} must be relative to the repo root")
        if ".." in g.split("/"):
            raise RepoError(f"--{kind}-scope {g!r} escapes the repo root")
        if g == ".git" or g.startswith(".git/"):
            raise RepoError(f"--{kind}-scope {g!r} targets .git")
        out.append(glob_to_re(g))
    return out


def preflight_repo(repo, wt_path):
    """Refuse at launch, not retroactively. Returns the base commit SHA."""
    if not os.path.isdir(repo):
        raise RepoError(f"--repo is not a directory: {repo}")
    try:
        git(repo, "rev-parse", "--git-dir")
    except RepoError:
        raise RepoError(f"--repo is not a git repository: {repo}")
    if os.path.exists(wt_path):
        raise RepoError(f"worktree path already exists: {wt_path}\n"
                        f"A crashed earlier run leaves one behind. Clean up with:\n"
                        f"  git -C {repo} worktree remove --force {wt_path}")
    # A dirty main tree is fine: the worktree is built from HEAD and is independent of it.
    return git(repo, "rev-parse", "HEAD")


def carry_ignored(repo, wt, paths):
    """cp -R gitignored assets into the worktree. NEVER symlink: symlinking into a
    worktree once left the SOURCE tree holding self-referential symlinks after
    `git worktree remove`, with the real directories no longer reachable there."""
    import shutil
    for rel in paths:
        src = os.path.join(repo, rel)
        if not os.path.exists(src):
            raise RepoError(f"--carry-ignored path does not exist: {src}")
        dst = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(dst) or wt, exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False)
        else:
            shutil.copy2(src, dst)


def settle_repo(repo, wt, base, allow_delete, mode, patch_path):
    """The authoritative gate. Diffs the worktree against the RECORDED BASE, never against
    worktree HEAD, so a model that commits, amends or checks out cannot hide a change."""
    git(wt, "add", "-A")
    artifact_re = [glob_to_re(g) for g in ARTIFACT_GLOBS]
    staged = git(wt, "diff", "--cached", "--name-only", base).splitlines()
    artifacts = sorted(p for p in staged if in_scope(p, artifact_re))
    if artifacts:
        git(wt, "reset", "-q", "--", *artifacts)
    ns = git(wt, "diff", "--cached", "--no-renames", "--name-status", base)
    files, violations = [], []
    for line in ns.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        files.append({"status": status, "path": path})
        if not in_scope(path, WRITE_SCOPE):
            violations.append({"path": path, "status": status,
                               "why": "outside write scope"})
        elif status.startswith("D") and not allow_delete:
            violations.append({"path": path, "status": status,
                               "why": "deletion without --allow-delete"})

    res = {"settled": False, "base": base, "files": files, "violations": violations,
           "artifacts": artifacts, "patch": None, "worktree": wt}
    if violations:
        res["reason"] = "scope_violation"
        with open(wt + ".violations.json", "w") as f:
            json.dump(res, f, indent=2)
        return res
    if not files:
        # An empty diff is a failure, not a pass. `did_write` only sees write_file, so a
        # run_command-only session would otherwise report a false completion.
        res["reason"] = "no_diff"
        return res

    if mode == "patch":
        blob = git(wt, "diff", "--cached", "--binary", base, binary=True)
        with open(patch_path, "wb") as f:
            f.write(blob)
        res["patch"] = patch_path
    elif mode == "branch":
        branch = "smith/" + os.path.basename(wt)
        git(wt, "checkout", "-b", branch)
        git(wt, "-c", "user.name=smith_agent",
            "-c", "user.email=smith@local", "commit", "-m",
            "smith_agent draft (unverified)")
        res["branch"] = branch
    res["settled"] = True
    res["reason"] = "ok"
    return res


def teardown_worktree(repo, wt, carried):
    """Remove the worktree ONLY after a clean pass, and only once the patch is written."""
    listing = None
    if carried:
        # `du` reports 0B for a broken symlink and reads as success; `ls -la` does not.
        listing = subprocess.run(["ls", "-la", *[os.path.join(repo, c) for c in carried]],
                                 capture_output=True, text=True).stdout
    git(repo, "worktree", "remove", "--force", wt)
    if carried:
        after = subprocess.run(["ls", "-la", *[os.path.join(repo, c) for c in carried]],
                               capture_output=True, text=True).stdout
        if after != listing:
            raise RepoError("carried assets in the SOURCE tree changed across worktree "
                            "removal — inspect before trusting them:\n" + after)


# ---------------------------------------------------------------- explore output + fan-out

def _data_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def write_report(args, report, ts):
    """Write the explore report where it can be reviewed and verdicted later."""
    path = args.report_out
    if not path:
        if os.environ.get("SMITH_NO_ARCHIVE") in ("1", "true", "yes"):
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{ts}-smith_explore-{args.model}")
        path = os.path.join(_data_dir(), "outputs", ts[:10],
                            f"{safe}-{uuid.uuid4().hex[:6]}.md")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            f.write(report + "\n")
        return path
    except OSError as exc:
        print(f"[explore] could not write report to {path}: {exc}", file=sys.stderr)
        return None


def parse_questions(text):
    """Questions separated by lines of exactly `---`; with none, one question per line
    (blank lines and `#` comments skipped)."""
    if re.search(r"(?m)^---[ \t]*$", text):
        parts = re.split(r"(?m)^---[ \t]*$", text)
    else:
        parts = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return [q.strip() for q in parts if q.strip()]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "question"


def _is_metered(args):
    """A route that bills per call. Fan-out multiplies it, so it needs --allow-metered.
    Free: local ollama, and the flat-rate z.ai Coding Plan. Metered: the Gemini API,
    Ollama Cloud (`name:cloud` and `name:size-cloud`), and any other openai host."""
    if args.backend == "ollama":
        tag = args.model.rsplit(":", 1)[1] if ":" in args.model else ""
        return tag == "cloud" or tag.endswith("-cloud")
    if args.backend == "openai":
        host = args.base_url.rstrip("/")
        return not (host == ZAI_CODING_URL or host.startswith(("http://localhost",
                                                                "http://127.0.0.1")))
    return True


def run_fanout(args, ap):
    """One explore agent per question, run as parallel child processes.

    Children, not threads: smith_agent keeps its run state in module globals, and a child
    per question also gives each its own transcript and ledger row. Success is judged from
    the ARTIFACT, not the child's word: a question only counts as ok if its report file
    exists and is non-empty. (`--batch` once reported 4/4 ok while writing 1 file.)"""
    try:
        with open(args.fanout) as f:
            questions = parse_questions(f.read())
    except OSError as exc:
        ap.error(f"--fanout: cannot read {args.fanout}: {exc}")
    if not questions:
        ap.error(f"--fanout: no questions in {args.fanout}")
    if _is_metered(args) and not args.allow_metered:
        ap.error(f"--fanout on {args.backend}/{args.model} bills per call, {len(questions)} "
                 f"agents x up to {args.max_turns} turns each. Use the flat-rate default "
                 f"(omit --model/--backend) or local ollama, or pass --allow-metered.")
    zai = args.backend == "openai" and args.base_url.rstrip("/") == ZAI_CODING_URL
    jobs = args.jobs or (4 if zai else 1)
    if zai and jobs > ZAI_MAX_JOBS:
        print(f"[fanout] -j {jobs} capped to {ZAI_MAX_JOBS}: z.ai 429s past ~6 requests in "
              f"flight", file=sys.stderr)
        jobs = ZAI_MAX_JOBS
    jobs = max(1, min(jobs, len(questions)))

    stamp = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = os.path.abspath(args.out_dir or os.path.join(
        _data_dir(), "outputs", time.strftime("%Y-%m-%d"), "fanout-" + stamp))
    os.makedirs(out, exist_ok=True)
    root = os.path.realpath(args.explore)

    names = [f"q{i:02d}-{_slug(q)}" for i, q in enumerate(questions, 1)]
    env = dict(os.environ, SMITH_FANOUT_ID=os.path.basename(out))

    def child(i):
        name, q = names[i], questions[i]
        qfile = os.path.join(out, name + ".question.txt")
        with open(qfile, "w") as f:
            f.write(q + "\n")
        cmd = [sys.executable, "-B", os.path.abspath(__file__), "--explore", root,
               "--prompt-file", qfile, "--report-out", os.path.join(out, name + ".md"),
               "--transcript", os.path.join(out, name + ".jsonl"),
               "--model", args.model, "--backend", args.backend,
               "--base-url", args.base_url, "--completions-path", args.completions_path,
               "--max-turns", str(args.max_turns), "--num-ctx", str(args.num_ctx),
               "--max-gen-tokens", str(args.max_gen_tokens), "--tag", args.tag]
        if args.api_key_env:
            cmd += ["--api-key-env", args.api_key_env]
        if args.think:
            cmd += ["--think", args.think]
        t = time.time()
        try:
            # stdin=DEVNULL: a child that ever reads stdin must not hang on the parent's.
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=args.child_timeout, env=env)
            lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
            res = json.loads(lines[-1]) if lines else {
                "finished": False, "stop": f"no_summary:exit-{p.returncode}",
                "stderr": (p.stderr or "")[-400:]}
        except subprocess.TimeoutExpired:
            res = {"finished": False, "stop": f"timeout:{args.child_timeout}s"}
        except (OSError, ValueError) as exc:
            res = {"finished": False, "stop": f"error:{exc}"}
        res.setdefault("seconds", round(time.time() - t, 1))
        rpath = os.path.join(out, name + ".md")
        res["ok"] = bool(res.get("finished")) and os.path.isfile(rpath) \
            and os.path.getsize(rpath) > 1
        if res.get("finished") and not res["ok"]:
            res["stop"] = "report_missing"
        tag = "ok  " if res["ok"] else "FAIL"
        print(f"[fanout] {tag} {name}  {res.get('turns', '?')} turns, {res['seconds']}s, "
              f"stop={res.get('stop')}", file=sys.stderr, flush=True)
        return res

    print(f"[fanout] {len(questions)} questions, {jobs} at a time, {args.model} -> {out}",
          file=sys.stderr, flush=True)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(child, range(len(questions))))

    combined = os.path.join(out, "fanout.md")
    with open(combined, "w") as f:
        f.write(f"# Fan-out: {len(questions)} questions on {root}\n\n"
                f"model {args.model} ({args.backend}), {jobs} parallel, "
                f"{round(time.time() - t0)}s. Each answer is a fleet DRAFT: check the "
                f"load-bearing citations before acting on it.\n\n")
        for name, q, res in zip(names, questions, results):
            cites = res.get("cites") or {}
            f.write(f"## {name}\n\n**Q:** {q}\n\n")
            f.write(f"_{'ok' if res['ok'] else 'FAILED'} · {res.get('turns', '?')} turns · "
                    f"{res.get('seconds')}s · stop={res.get('stop')} · citations "
                    f"{cites.get('valid', 0)} valid / {cites.get('invalid', 0)} invalid"
                    f"{' · ts ' + res['ts'] if res.get('ts') else ''}_\n\n")
            if cites.get("invalid_examples"):
                f.write("⚠ citations that point at no such file/line: "
                        + ", ".join(f"`{c}`" for c in cites["invalid_examples"]) + "\n\n")
            rpath = os.path.join(out, name + ".md")
            if res["ok"]:
                with open(rpath) as r:
                    f.write(r.read().rstrip() + "\n\n")
            else:
                f.write("(no report)\n\n")
    failed = [{"q": n, "stop": r.get("stop")} for n, r in zip(names, results) if not r["ok"]]
    print(json.dumps({"fanout": len(questions), "ok": len(questions) - len(failed),
                      "failed": failed, "jobs": jobs, "model": args.model,
                      "seconds": round(time.time() - t0, 1), "out_dir": out,
                      "combined": combined}))
    return 1 if failed else 0


def main():
    global MAX_GEN_TOKENS, THINK, WRITE_SCOPE, READ_SCOPE, REPO_MODE, EXPLORE, TOOLS
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="required, unless the contract supplies it")
    ap.add_argument("--workdir", default=None,
                    help="sandbox mode: a throwaway directory the model owns entirely")
    ap.add_argument("--repo", default=None,
                    help="repo mode: a real git repo. Runs in a disposable worktree under "
                         "--write-scope and settles to a patch. Excludes --workdir.")
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--contract", default=None,
                    help="task contract: --- front matter (model, write_scope, verify, "
                         "...) then the task prose. Replaces --prompt-file.")
    ap.add_argument("--write-scope", action="append", default=None, metavar="GLOB",
                    help="repo mode, repeatable, REQUIRED. `**` spans directories.")
    ap.add_argument("--read-scope", action="append", default=None, metavar="GLOB",
                    help="repo mode, repeatable. ADVISORY: filters list_files only. "
                         "run_command has a shell, so this is not a boundary.")
    ap.add_argument("--allow-delete", action="store_true",
                    help="permit deletions in the settled diff (default: refuse)")
    ap.add_argument("--carry-ignored", action="append", default=None, metavar="PATH",
                    help="cp -R a gitignored asset into the worktree. Never symlinked.")
    ap.add_argument("--settle", choices=["patch", "branch", "none"], default="patch",
                    help="patch (default) writes a .patch and removes the worktree; "
                         "branch commits on a smith/* branch; none just reports.")
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--backend", choices=["ollama", "gemini", "openai"], default=None,
                    help="default ollama; --explore defaults to openai + zai-coding")
    ap.add_argument("--base-url", default="http://localhost:8080",
                    help="openai backend server (e.g. mlx_lm server, or a cloud host). "
                         "'zai-coding' = the flat-rate z.ai GLM Coding Plan (sets the path "
                         "and ZAI_API_KEY for you)")
    ap.add_argument("--api-key-env", default=None,
                    help="env var holding a Bearer token for the openai backend "
                         "(e.g. ZAI_API_KEY); omitted = 'local' for unauthenticated "
                         "local servers")
    ap.add_argument("--completions-path", default="/v1/chat/completions",
                    help="path appended to --base-url for the openai backend. Default "
                         "matches mlx_lm/LM Studio; override for hosts that don't follow "
                         "the /v1/chat/completions convention (e.g. z.ai: /chat/completions)")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--max-gen-tokens", type=int, default=None,
                    help="per-turn generation cap (default 1600, --explore 16384). Raise "
                         "for tasks that write large single files.")
    ap.add_argument("--tag", default=None, metavar="TASKSHAPE",
                    help="task-shape label for the ledger / hebbian router "
                         "(default: app-build, --explore subagent-fanout).")
    ap.add_argument("--finish-gate", action="store_true",
                    help="bounce the first finish call with a requirement-audit prompt "
                         "(measured null result on qwen2.5-coder:14b, 2026-07-01)")
    ap.add_argument("--think", choices=["on", "off", "low", "medium", "high", "max"],
                    default=None,
                    help="ollama backend only: send Ollama's `think` field (default: the "
                         "model's own). 'off' fixes deepseek-v4.1-flash's temp-0 reasoning "
                         "loop; gpt-oss ignores 'off' and needs a level such as 'low'.")
    ap.add_argument("--explore", default=None, metavar="DIR",
                    help="read-only explore mode on DIR: list/grep/read only, no write or "
                         "shell; ends with a cited markdown report. Excludes --repo/--workdir.")
    ap.add_argument("--question", default=None,
                    help="explore mode: the question, instead of --prompt-file")
    ap.add_argument("--report-out", default=None,
                    help="explore mode: write the report here (default: archived under "
                         "data/outputs/ and also printed to stderr)")
    ap.add_argument("--fanout", default=None, metavar="FILE",
                    help="explore mode: one agent per question in FILE, in parallel. "
                         "Questions are separated by '---' lines, or one per line.")
    ap.add_argument("-j", "--jobs", type=int, default=None,
                    help=f"--fanout parallelism (default 4 on z.ai, capped at "
                         f"{ZAI_MAX_JOBS}; 1 on local ollama)")
    ap.add_argument("--out-dir", default=None,
                    help="--fanout: where reports go (default data/outputs/<date>/fanout-*)")
    ap.add_argument("--child-timeout", type=int, default=1800,
                    help="--fanout: seconds before one agent is killed (default 1800)")
    ap.add_argument("--allow-metered", action="store_true",
                    help="--fanout: permit a pay-per-use route (Gemini API, Ollama :cloud)")
    args = ap.parse_args()

    # Contract supplies defaults; an explicit CLI flag always wins.
    meta, task = {}, None
    try:
        if args.contract:
            meta, task = parse_contract(args.contract)
            for k, v in meta.items():
                if k != "verify" and hasattr(args, k) and getattr(args, k) == ap.get_default(k):
                    setattr(args, k, v)
    except RepoError as exc:
        ap.error(str(exc))

    if sum(bool(x) for x in (args.repo, args.workdir, args.explore)) != 1:
        ap.error("give exactly one of --repo (a real repository), --workdir (a sandbox) "
                 "or --explore (read-only investigation)")
    EXPLORE = bool(args.explore)
    if (args.fanout or args.question or args.report_out) and not EXPLORE:
        ap.error("--fanout/--question/--report-out only apply to --explore")
    if EXPLORE and not args.model and not args.backend:
        # Bulk default is the flat-rate GLM Coding Plan: already paid for, $0 marginal,
        # and the lane that scored 10/10 on the first read+summarize fan-out trial.
        args.backend, args.base_url, args.model = "openai", "zai-coding", "glm-5.3"
        print("[route] --explore defaults to z.ai glm-5.3 (flat-rate Coding Plan)",
              file=sys.stderr)
    args.backend = args.backend or "ollama"
    if args.base_url == "zai-coding":
        args.base_url = ZAI_CODING_URL
        if args.completions_path == ap.get_default("completions_path"):
            args.completions_path = "/chat/completions"
        args.api_key_env = args.api_key_env or "ZAI_API_KEY"
    args.tag = args.tag or ("subagent-fanout" if EXPLORE else "app-build")
    if args.max_gen_tokens is None:
        args.max_gen_tokens = 16384 if EXPLORE else MAX_GEN_TOKENS
    if not args.model:
        ap.error("--model is required (pass the flag, or set it in the contract)")
    if args.fanout:
        if args.question or args.prompt_file or args.contract:
            ap.error("--fanout reads its questions from FILE; drop --question/--prompt-file")
        sys.exit(run_fanout(args, ap))
    if task is None:
        if args.question:
            task = args.question
        elif not args.prompt_file:
            ap.error("give --prompt-file or --contract" +
                     (" (or --question)" if EXPLORE else ""))
        else:
            with open(args.prompt_file) as f:
                task = f.read()
    if meta.get("verify"):
        task += ("\n\nVerify your change by running exactly this command, and do not call "
                 "finish until it passes:\n    " + meta["verify"])
    if not args.repo and (args.write_scope or args.read_scope or args.carry_ignored):
        ap.error("--write-scope/--read-scope/--carry-ignored only apply to --repo")
    if args.repo and not args.write_scope:
        # Refuse at launch, before a worktree exists: an unscoped run has no gate.
        ap.error("--repo requires at least one --write-scope glob (or write_scope in the "
                 "contract); an unscoped run has no gate")

    MAX_GEN_TOKENS = args.max_gen_tokens
    if args.think and args.backend != "ollama":
        ap.error(f"--think only applies to --backend ollama (got --backend {args.backend})")
    THINK = {"on": True, "off": False}.get(args.think, args.think)

    repo = wt = base = patch_path = None
    carried = args.carry_ignored or []
    REPO_MODE = bool(args.repo)
    WRITE_SCOPE = READ_SCOPE = None
    SCOPE_SRC.clear()
    system = SYSTEM_EXPLORE if EXPLORE else SYSTEM
    if EXPLORE:
        TOOLS = EXPLORE_TOOLS
        workdir = os.path.realpath(args.explore)
        if not os.path.isdir(workdir):
            ap.error(f"--explore: not a directory: {args.explore}")
    elif args.repo:
        repo = os.path.realpath(args.repo)
        wtroot = os.environ.get("SMITH_WORKTREES") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "worktrees")
        wt = os.path.join(wtroot, "wt-" + time.strftime("%Y%m%d-%H%M%S")
                          + "-" + uuid.uuid4().hex[:6])
        patch_path = wt + ".patch"
        try:
            WRITE_SCOPE = validate_scope(args.write_scope, "write")
            SCOPE_SRC["write"] = args.write_scope
            if args.read_scope:
                # Union in the write scope: hiding the files the task must edit is never
                # what the author meant by a narrow read scope.
                READ_SCOPE = validate_scope(args.read_scope, "read") + WRITE_SCOPE
                SCOPE_SRC["read"] = args.read_scope
            base = preflight_repo(repo, wt)
            os.makedirs(wtroot, exist_ok=True)
            git(repo, "worktree", "add", "--detach", wt, base)
        except RepoError as exc:
            print(json.dumps({"finished": False, "settled": False, "turns": 0,
                              "seconds": 0.0, "stop": "preflight", "error": str(exc)}))
            sys.exit(2)
        try:
            if carried:
                carry_ignored(repo, wt, carried)
        except RepoError as exc:
            git(repo, "worktree", "remove", "--force", wt)
            print(json.dumps({"finished": False, "settled": False, "turns": 0,
                              "seconds": 0.0, "stop": "preflight", "error": str(exc)}))
            sys.exit(2)
        workdir = wt
    else:
        workdir = os.path.realpath(args.workdir)

    tlog = None
    if args.transcript:
        os.makedirs(os.path.dirname(args.transcript) or ".", exist_ok=True)
        tlog = open(args.transcript, "a")

    def log(kind, data):
        if tlog:
            tlog.write(json.dumps({"t": round(time.time(), 2), "kind": kind, "data": data}) + "\n")
            tlog.flush()

    backend = args.backend
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    messages = [{"role": "system", "content": system},
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
    native_ok = True  # tools= flag; flips False after an ollama tool-parse 500 -> fallback
    did_write = False  # gates `finished`: a real write_file must succeed this session
    report = ""        # explore mode: the answer, from finish(report=...) or a prose reply
    nudge = NUDGE_EXPLORE if EXPLORE else NUDGE
    # Explore gets one extra LAST-CALL turn: an agent that spent its budget reading still
    # knows things, and "out of turns, nothing returned" throws all of it away.
    limit = args.max_turns + (1 if EXPLORE else 0)
    last_call = False
    for turn in range(1, limit + 1):
        if EXPLORE and turn == limit:
            last_call = True
            push_user("[system note] Your turn budget is used up. Call finish NOW with your "
                      "report of what you found so far, citing path:line, and say plainly "
                      "what you did not get to verify. No more searching.")
            log("last_call", {"turn": turn})
        try:
            if backend == "gemini":
                parts = chat_gemini(args.model, contents, system)
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
                msg = gemini_parts_to_msg(parts)
            elif backend == "openai":
                msg = chat_openai(args.model, messages, args.base_url, tools=native_ok,
                                  api_key=api_key, completions_path=args.completions_path)
                messages.append(msg)
            else:
                resp = chat(args.model, messages, args.num_ctx, tools=native_ok)
                msg = resp.get("message", {})
                messages.append(msg)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            if (backend == "ollama" and native_ok and exc.code == 500
                    and "parsing tool call" in body):
                # SELF-HEAL: Ollama's server-side tool parser choked (truncated or
                # complex call — the known killer). Drop native tool schemas for the
                # REST OF THE SESSION and continue via the JSON-fallback protocol,
                # which our client-side parser handles gracefully. The failed turn's
                # output is lost, but the session survives.
                native_ok = False
                push_user("[system note] Native tool calling is failing on this "
                          "server. From now on respond with exactly ONE tool call "
                          "per turn as a ```json block: "
                          '{"name": ..., "arguments": {...}}. ' + WRITE_CONVENTION)
                log("self_heal", {"turn": turn, "reason": body[:200]})
                continue
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
            prose = (msg.get("content") or "").strip()
            if EXPLORE and (last_call or len(prose) >= 300):
                # A substantive prose answer IS the report; the stop label keeps it visible.
                report, stop = prose, "no_tools"
                break
            if not nudged and not last_call:
                # One shot at teaching the protocol before giving up.
                nudged = True
                push_user(nudge)
                log("nudge", nudge)
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
                if EXPLORE:
                    report = str(raw_args.get("report") or raw_args.get("summary") or "")
            elif EXPLORE and name in ("write_file", "run_command"):
                result = ("ERROR: this is a read-only investigation; only list_files, grep, "
                          "read_file and finish exist")
            elif EXPLORE and name == "list_files":
                result = t_x_list(workdir, raw_args.get("path", "."), raw_args.get("glob"))
            elif EXPLORE and name == "read_file":
                result = t_x_read(workdir, raw_args.get("path", ""),
                                  raw_args.get("start_line"), raw_args.get("end_line"))
            elif EXPLORE and name == "grep":
                result = t_x_grep(workdir, raw_args.get("pattern", ""),
                                  raw_args.get("path", "."), raw_args.get("glob"),
                                  bool(raw_args.get("ignore_case")))
            elif name == "list_files":
                result = t_list_files(workdir, raw_args.get("path", "."))
            elif name == "read_file":
                result = t_read_file(workdir, raw_args.get("path", ""))
            elif name == "write_file":
                result = t_write_file(workdir, raw_args.get("path", ""),
                                      raw_args.get("content", ""))
                if result.startswith("OK:"):
                    did_write = True
            elif name == "run_command":
                result = t_run_command(workdir, raw_args.get("command", ""))
            else:
                result = f"ERROR: unknown tool {name}"

            if backend == "gemini" and native:
                tool_parts.append({"functionResponse":
                                   {"name": name, "response": {"result": result}}})
            elif backend == "openai" and native:
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": result})
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
                      ("\n\nContinue. Respond with your next single tool call as a "
                       "```json block, or call finish with your report when you can answer."
                       if EXPLORE else
                       "\n\nContinue. Respond with your next single tool call as a "
                       "```json block, or call finish when done and verified. " +
                       WRITE_CONVENTION))

    # A finish (explicit or prose-only give-up) only counts if a write_file actually
    # landed this session — otherwise it's a false-positive completion (the model
    # talked, verified nothing, changed nothing). Every real task needs a write.
    if EXPLORE:
        # Explore writes nothing by design; what it owes is a non-empty answer.
        report = report.strip()
        really_finished = bool(report)
        if not report and stop in ("finish", "no_tools"):
            stop = f"{stop}_empty_report"
        elif report and last_call:
            stop = f"{stop}_last_call"
    else:
        really_finished = (finished or stop == "no_tools") and did_write
        if not really_finished and stop in ("finish", "no_tools"):
            stop = f"{stop}_no_write"
    summary = {"finished": really_finished, "turns": turn,
               "seconds": round(time.time() - t0, 1), "stop": stop}
    if EXPLORE:
        # A run's identity everywhere downstream is (ts, script, model), and parallel
        # fan-out children finish in the same second (measured: 3 of 5 on the first live
        # run), so one verdict would grade all of them. Microseconds keep each run its own.
        summary["ts"] = datetime.datetime.now().isoformat(timespec="microseconds")
        summary["report"] = write_report(args, report, summary["ts"]) if report else None
        summary["report_chars"] = len(report)
        summary["cites"] = check_citations(workdir, report)

    if REPO_MODE:
        # The DIFF is the truth here: it sees the run_command writes that did_write never
        # sees, and it is taken against the RECORDED BASE, never against worktree HEAD.
        try:
            res = settle_repo(repo, wt, base, args.allow_delete, args.settle, patch_path)
        except RepoError as exc:
            res = {"settled": False, "reason": "settle_error: " + str(exc), "base": base,
                   "files": [], "violations": [], "artifacts": [], "patch": None}
        summary["settled"] = res["settled"]
        summary["base"] = res["base"]
        summary["patch"] = res["patch"]
        summary["files"] = [f"{f['status']}\t{f['path']}" for f in res["files"]]
        summary["violations"] = res["violations"]
        if res.get("artifacts"):
            summary["artifacts"] = res["artifacts"]
        summary["finished"] = res["settled"]
        if res.get("branch"):
            summary["branch"] = res["branch"]
        if not res["settled"]:
            # Never let the settle reason MASK a loop failure: a 404 that produced no diff
            # is an error, not "the model changed nothing", and relabelling it reads as a
            # clean no-op run.
            summary["loop_stop"] = stop
            if not stop.startswith("error:"):
                summary["stop"] = {"no_diff": "finish_no_diff",
                                   "scope_violation": "scope_violation"}.get(
                                       res["reason"], res["reason"])
            summary["worktree"] = wt          # kept, so the failure can be inspected
        elif args.settle == "patch":
            try:
                teardown_worktree(repo, wt, carried)
            except RepoError as exc:
                summary["teardown_warning"] = str(exc)
                summary["worktree"] = wt
        else:
            summary["worktree"] = wt
    log("summary", summary)
    if tlog:
        tlog.close()
    if EXPLORE and report and not args.report_out:
        print(report, file=sys.stderr)
    print(json.dumps(summary))
    led = dict(summary)
    # Keep the ledger one-line-per-run: counts, not the whole file list.
    if REPO_MODE:
        led["files"] = len(summary["files"])
        led["violations"] = len(summary["violations"])
        led["artifacts"] = len(summary.get("artifacts", []))
    if EXPLORE:
        led["output_file"] = led.pop("report")
        led["cites"] = f"{summary['cites']['valid']}/{summary['cites']['invalid']}"
        if os.environ.get("SMITH_FANOUT_ID"):
            led["fanout"] = os.environ["SMITH_FANOUT_ID"]
    _ledger({"script": "smith_agent", "backend": backend, "model": args.model,
             "tag": args.tag, "think": args.think, "workdir": workdir, "task": task[:120],
             **led})


if __name__ == "__main__":
    main()
