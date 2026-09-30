#!/usr/bin/env python3
"""jev.py — Jev (typesafe/jev-1.13), a calibrated classification model on OpenRouter's
Decisions API. NOT a chat model: it answers predefined yes/no (noul), multiple-choice
(choice), or scored (score) questions about a piece of text and returns calibrated
probabilities — it cannot write code or free text. Wrong tool if that's what's wanted;
see gemini.py for that.

Endpoint: POST https://openrouter.ai/api/alpha/decisions (alpha, added 2026-09-18).
Auth: OPENROUTER_API_KEY (Bearer). Docs: openrouter.ai/docs/guides/community/jev.

First real use (2026-09-30): a Bibliome misfiling-detector pilot
(~/Python/pdf_organizer/jev_misfiling_pilot.py) — $0.000062/doc average, see the
jev-openrouter-decisions-api memory note.

USAGE
-----
The general form — one or more questions, asked together in ONE call (cheaper than N
separate calls when they're about the same piece of text):

    python3 jev.py --tag classify --state "some text" \\
        --questions-file questions.json
    # questions.json: {"is_bug": {"type": "noul", "instructions": "...",
    #                              "criteria": {"true": "...", "false": "..."}}, ...}

    echo "some text" | python3 jev.py --tag classify --questions '{"...": {...}}'

Shorthand for the common single-question case (question name is always "answer"):

    python3 jev.py --tag classify --state "some text" \\
        --noul "Is this a greeting?" --true "A greeting like hello." --false "Not one."

    python3 jev.py --tag classify --state "some text" \\
        --choice "Which team owns this?" \\
        --criteria '{"frontend": "UI bugs", "backend": "server bugs"}'

    python3 jev.py --tag classify --state "some text" \\
        --score "How urgent?" --levels '["can wait", "this week", "now"]'

The full JSON response (answers, probabilities, usage.cost) prints to stdout; a
one-line summary and the real cost print to stderr. Every call is logged to the
skill's usage ledger like every other script here — pass --tag (required) so it shows
up in gap_report.py; untagged calls don't exist for routing purposes.
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
_gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gemini)
log = _gemini.log
_ledger = _gemini._ledger

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"


def api_key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        log("ERROR: OPENROUTER_API_KEY is not set. Ask the user to export it, then retry.")
        sys.exit(2)
    return k


def call_jev(model, questions, state, *, session_id=None, user=None):
    """POST to the Decisions endpoint. Returns the parsed response dict.

    Retries 429/500/502/503 like gemini.py's other HTTP paths; every other HTTP error
    is fatal and prints OpenRouter's own message (it's specific — e.g. the exact
    "is a decisions model, use /api/alpha/decisions" 400 that started this file)."""
    body = {"model": model, "questions": questions, "state": state}
    if session_id:
        body["session_id"] = session_id
    if user:
        body["user"] = user
    headers = {"Content-Type": "application/json", "User-Agent": "agent-smith/1.4",
               "Authorization": f"Bearer {api_key()}"}
    for attempt in range(3):
        req = urllib.request.Request(ENDPOINT, method="POST",
                                     data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                wait = 4 * (attempt + 1)
                log(f"HTTP {e.code} from OpenRouter; retry in {wait}s ...")
                time.sleep(wait)
                continue
            log(f"ERROR: {ENDPOINT} HTTP {e.code}: {msg}")
            sys.exit(1)
        except urllib.error.URLError as e:
            log(f"ERROR: can't reach OpenRouter ({e}).")
            sys.exit(1)


def _summarize_answer(name, a):
    t = a.get("type")
    if t == "noul":
        return f"{name}={a.get('noul')}"
    if t == "choice":
        return f"{name}={a.get('choice')} (conf={a.get('confidence')})"
    if t == "score":
        return f"{name}={a.get('score')} (conf={a.get('confidence')})"
    return f"{name}=?"


def build_shorthand_question(args):
    """One of --noul/--choice/--score into the single-question {"answer": {...}} shape.
    Returns None if none of the shorthand flags were used (caller falls through to
    --questions/--questions-file)."""
    if args.noul:
        if not (args.true and args.false):
            log("ERROR: --noul needs both --true and --false (what each answer means).")
            sys.exit(2)
        return {"answer": {"type": "noul", "instructions": args.noul,
                           "criteria": {"true": args.true, "false": args.false}}}
    if args.choice:
        if not args.criteria:
            log("ERROR: --choice needs --criteria (a JSON object of choice -> description).")
            sys.exit(2)
        try:
            criteria = json.loads(args.criteria)
        except json.JSONDecodeError as e:
            log(f"ERROR: --criteria is not valid JSON: {e}")
            sys.exit(2)
        return {"answer": {"type": "choice", "instructions": args.choice, "criteria": criteria}}
    if args.score:
        if not args.levels:
            log("ERROR: --score needs --levels (a JSON array of level descriptions, ordered).")
            sys.exit(2)
        try:
            levels = json.loads(args.levels)
        except json.JSONDecodeError as e:
            log(f"ERROR: --levels is not valid JSON: {e}")
            sys.exit(2)
        return {"answer": {"type": "score", "instructions": args.score, "criteria": levels}}
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--state", help="the text to classify (or pipe it via stdin)")
    ap.add_argument("--state-file", help="read --state from this file instead")
    ap.add_argument("--questions", help="the full questions dict, as a JSON string")
    ap.add_argument("--questions-file", help="the full questions dict, as a JSON file")
    ap.add_argument("--noul", metavar="INSTRUCTIONS", help="shorthand: one yes/no question")
    ap.add_argument("--true", help="with --noul: what a true answer means")
    ap.add_argument("--false", help="with --noul: what a false answer means")
    ap.add_argument("--choice", metavar="INSTRUCTIONS", help="shorthand: one multiple-choice question")
    ap.add_argument("--criteria", help="with --choice: JSON object {choice: description}")
    ap.add_argument("--score", metavar="INSTRUCTIONS", help="shorthand: one scored question")
    ap.add_argument("--levels", help="with --score: JSON array of ordered level descriptions")
    ap.add_argument("--session-id", help="passthrough for OpenRouter tracing/observability")
    ap.add_argument("--purpose", help="human-readable 'what was this for', for the ledger")
    ap.add_argument("--tag", required=True,
                    help="task-shape label for the usage ledger/gap_report.py — required, "
                         "an untagged call is invisible to routing decisions")
    args = ap.parse_args()

    questions = build_shorthand_question(args)
    if questions is None:
        if args.questions_file:
            with open(args.questions_file) as f:
                questions = json.load(f)
        elif args.questions:
            try:
                questions = json.loads(args.questions)
            except json.JSONDecodeError as e:
                log(f"ERROR: --questions is not valid JSON: {e}")
                sys.exit(2)
        else:
            log("ERROR: give one question shape: --noul / --choice / --score, or the "
                "general --questions / --questions-file.")
            sys.exit(2)

    if args.state_file:
        with open(args.state_file) as f:
            state = f.read()
    elif args.state:
        state = args.state
    elif not sys.stdin.isatty():
        state = sys.stdin.read()
    else:
        log("ERROR: give --state, --state-file, or pipe the text to classify via stdin.")
        sys.exit(2)

    t0 = time.time()
    resp = call_jev(args.model, questions, state, session_id=args.session_id)
    answers = resp.get("answers") or {}
    usage = resp.get("usage") or {}
    seconds = round(time.time() - t0, 1)

    print(json.dumps(resp, indent=2))
    sys.stdout.flush()

    summary = "; ".join(_summarize_answer(k, v) for k, v in answers.items())
    log(f"\n--- jev meta ---")
    log(f"model: {resp.get('model', args.model)}  cost: ${usage.get('cost', 0):.6f}  "
        f"tokens: in={usage.get('input_tokens')} out={usage.get('output_tokens')}  "
        f"{seconds}s")
    log(f"answers: {summary}")

    _ledger({"script": "jev", "model": resp.get("model", args.model), "tag": args.tag,
             "purpose": args.purpose, "questions": list(questions.keys()),
             "state_chars": len(state), "cost": usage.get("cost"),
             "input_tokens": usage.get("input_tokens"),
             "output_tokens": usage.get("output_tokens"),
             "answers": {k: v.get(v.get("type")) for k, v in answers.items()},
             "seconds": seconds, "status": "ok"},
            output=json.dumps(resp))


if __name__ == "__main__":
    main()
