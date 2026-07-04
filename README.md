# Agent Smith

A Claude Code skill that **offloads bulky, repetitive work to a cheaper model — then has Claude
verify and finish it** — so you spend Claude's tokens on judgment, not grunt work.

Gemini (or a local model) drafts the words: research, document digests, bulk extraction/transform,
plans, marketing copy, config boilerplate, first-draft code. Claude scopes the task, cross-checks
the output, and integrates it. The model drafts; **Claude verifies.** Nothing the model writes is
treated as a deliverable until it's been reviewed.

> Named for the Matrix agent who copies himself across the system — this skill fans heavy work out
> to a fleet of model "copies" while the One keeps the judgment.

## What it's good for

- **Web research** with source links (Gemini's Google Search grounding)
- **Digesting big inputs** — summarize/extract/classify long PDFs, transcripts, logs, CSVs
- **Bulk transforms** — classify or rewrite many records
- **First drafts** — plans, proposals, marketing copy, config/IaC boilerplate, code + tests
- **Code from a spec** — a new module, class, CLI tool, or test suite; porting/translating code
- **Agentic sandbox builds** — fix-a-bug / add-a-feature / build-a-small-app: `smith_agent.py`
  runs a local model in a sandboxed tool loop (list/read/write/run/finish) until its own
  verification passes
- **Screenshot triage (local vision)** — `--file shot.png --backend ollama`: UI QA, error
  dialogs, "what's broken here?" — free, private, unlimited
- **Batch sweeps** — `--batch manifest.txt`: one prompt over many files (text or images),
  per-item output files, ONE summary back — zero per-item orchestration cost
- **A usage ledger** — every run logs one JSON line (`data/usage.jsonl`); check what your
  fleet actually did with `scripts/usage_report.py`

It is **not** for short/interactive work, correctness-critical debugging, or the *execution* half of
a task (deploying, committing, posting) — those stay with Claude or a script you control.

## Install

**As a plugin (recommended):**

```
/plugin marketplace add negativetime/agent-smith-plugin
/plugin install agent-smith@agent-smith-marketplace
```

**As a personal skill (no plugin system):** copy `plugins/agent-smith/skills/agent-smith/` into
`~/.claude/skills/agent-smith/` (macOS/Linux) or `%USERPROFILE%\.claude\skills\agent-smith\` (Windows).

## Setup — pick ONE backend (or several)

### Option A — Gemini cloud (default, fastest & strongest)

Get a **free** API key at <https://aistudio.google.com/apikey>, then:

```bash
export GEMINI_API_KEY=your_key_here     # macOS/Linux (add to your shell profile to persist)
# Windows:  setx GEMINI_API_KEY "your_key_here"   (open a new shell after)
```

**Hitting free-tier rate limits (429s)?** If you have the [Gemini CLI](https://github.com/google-gemini/gemini-cli)
installed and logged in (`gemini` → *"Login with Google"*), use `--backend gemini-cli` to run the same
Gemini models on your **subscription/account quota instead of the metered API key** — no rate-limit ceiling.
It auto-disables the CLI's tools (text-only, never edits files) and runs ~25% leaner.

### Option B — No account, fully local (Ollama)

**Don't have a Gemini account? You don't need one.** Install [Ollama](https://ollama.com), make sure
`ollama serve` is running, then run the **disk-aware installer** — it sizes the model to your free space:

```bash
bash plugins/agent-smith/skills/agent-smith/scripts/setup_local_model.sh
```

It offers, by available disk:

| For | Model | ~Size | Notes |
|---|---|---|---|
| **Code** | `qwen3-coder:30b` | 18 GB | **recommended** — best local coder (30B MoE, 3B active, fast; benchmarked) |
| **Code** | `qwen2.5-coder:14b` | 9 GB | lighter, solid runner-up |
| **Code** | `qwen2.5-coder:7b` | 5 GB | smallest & fastest |
| **General / no-account Gemini alternative** | `gemma3:12b` | 8 GB | well-rounded |
| **General** | `gemma3:27b` | 17 GB | strongest Gemma |
| Light text | `llama3.2:3b` | 2 GB | tiny floor |

Then use `--backend ollama` (defaults to `qwen3-coder:30b`, or set `OLLAMA_MODEL`).

### Option C — Free cloud via the generic OpenAI socket

`--backend openai` speaks to **any OpenAI-compatible endpoint** — several have genuinely
useful free tiers. Standout: **Groq** hosts OpenAI's open-weight `gpt-oss-120b` free, at
extreme speed:

```bash
export GROQ_API_KEY=your_groq_key       # free at console.groq.com
python3 "$SKILL/scripts/gemini.py" --backend openai --base-url groq \
  --model openai/gpt-oss-120b "Draft a data-model for ..."
```

Shorthand base-urls: `groq` | `openrouter` | `openai` | `ollama` (or any full `.../v1` URL).
Auth is `OPENAI_API_KEY` (Groq: `GROQ_API_KEY`); local servers need none. Built-in 429 retry.
**Caveat: free cloud tiers commonly reserve the right to train on your data — keep private
work on the local backends.**

### Option D — Apple Foundation Models (advanced, opt-in)

The `fm` backend runs on-device on **macOS 26+ with Apple Intelligence**. **No binary ships with this
plugin** (don't run opaque executables from strangers) — you supply your own `fm_helper`: a tiny Swift
CLI wrapping Apple's `FoundationModels` framework that reads `{"messages":[...],"system":...}` JSON on
stdin and prints `{"answer": "..."}`. Point the skill at it with `export FM_HELPER=/path/to/fm_helper`.
If unset, the `fm` backend simply errors and you stay on `gemini`/`ollama`.

## Usage

Once a backend is set up, just ask Claude to do offload-shaped work ("research X with sources",
"summarize this 200-page PDF", "draft a wrangler.toml", "turn this announcement into posts"). The
skill triggers automatically. You can also be explicit: *"use Gemini for this."*

Under the hood Claude calls the helper, e.g.:

```bash
SKILL="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-smith}"
SKILL="${SKILL:-$HOME/.claude/skills/agent-smith}"

python3 "$SKILL/scripts/gemini.py" --search "What's new in the latest Python release?"
python3 "$SKILL/scripts/gemini.py" --file report.pdf "Summarize the findings as bullets"
python3 "$SKILL/scripts/gemini.py" --backend ollama --model qwen3-coder:30b "Draft a function that ..."

# local vision: what's wrong with this screenshot?
python3 "$SKILL/scripts/gemini.py" --backend ollama --file shot.png "Anything visibly broken?"

# batch: one prompt over many files, zero per-item cost
python3 "$SKILL/scripts/gemini.py" --backend ollama --batch files.txt --out-dir out "Classify: ..."

# agentic: build/fix something in a SCRATCH dir until its own tests pass
python3 "$SKILL/scripts/smith_agent.py" --model gpt-oss:20b --workdir /tmp/scratch --prompt-file task.txt

# what has the fleet been doing?
python3 "$SKILL/scripts/usage_report.py" --today
```

## Which model for what (measured, not vibes)

Every assignment below comes from the author's eval harness — hidden-test graded tasks
across code-gen, structured extraction, repo edits, app builds, and a blind-judged design
rubric. Trust is *earned per capability*, and re-earned when models change.

| Task | Model | Why |
|---|---|---|
| Quality code: modules, extraction, repo edits, **app builds** | `gpt-oss:20b` (13 GB, `ollama pull gpt-oss:20b`) | the only model to sweep the harness twice consecutively — incl. agentic app builds. Watch-item in review: reasoning residue (commented-out debug prints, dead branches) |
| Vision + design polish | `gemma4:26b` (17 GB) | reads screenshots/dialogs/charts exactly (window-sized; tile tall captures — small text gets confidently invented); held the blind design rubric 21 : 19.5 |
| Fast bulk drafts | `qwen3-coder:30b` (18 GB) | 2–8s one-shots vs the reasoners' 13–90s |
| Lighter machines | `qwen2.5-coder:14b` (9 GB) | solid runner-up at half the size |
| Tiny text bulk | `llama3.2:3b` (2 GB) | floor tier; not for code or agents |
| Research / big files / hardest cloud drafts | Gemini `--model pro` | still the ceiling; the only backend with file ingest + web grounding |
| Free frontier-adjacent burst | Groq `openai/gpt-oss-120b` via `--backend openai` | when you want big-model quality without touching Gemini quota |

- **Always:** the model drafts, **Claude (or you) verifies.** Every model tested — winners
  included — shipped at least one bug a review caught. The point isn't a perfect model;
  it's a pipeline where imperfection is caught before it counts.

## Platform support

- **Gemini / Ollama backends:** macOS, Linux, Windows (pure-stdlib Python helper).
- **Apple FM backend:** macOS 26+ only, and only with your own `fm_helper`.

## License

MIT — see [LICENSE](LICENSE).
