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
| **Code** | `qwen2.5-coder:7b` | 5 GB | small & fast |
| **Code** | `qwen2.5-coder:14b` | 9 GB | **recommended** — best balance (benchmarked) |
| **Code** | `qwen2.5-coder:32b` | 20 GB | best local code quality, if you have room |
| **General / no-account Gemini alternative** | `gemma3:12b` | 8 GB | well-rounded |
| **General** | `gemma3:27b` | 17 GB | strongest Gemma |
| Light text | `llama3.2:3b` | 2 GB | tiny floor |

Then use `--backend ollama` (defaults to `qwen2.5-coder:14b`, or set `OLLAMA_MODEL`).

### Option C — Apple Foundation Models (advanced, opt-in)

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
python3 "$SKILL/scripts/gemini.py" --backend ollama --model qwen2.5-coder:14b "Draft a function that ..."
```

## Which model for what (from the bundled coding bake-off)

- **Best coder overall:** Gemini `--model pro` (cloud) — swept correctness + design.
- **Best local/offline coder:** `qwen2.5-coder:14b` (ties a much larger model, half the size).
- **General text, no account:** Gemma (`gemma3:12b`/`27b`).
- **Always:** the model drafts, **you verify.** Every model tested shipped at least one bug a review caught.

## Platform support

- **Gemini / Ollama backends:** macOS, Linux, Windows (pure-stdlib Python helper).
- **Apple FM backend:** macOS 26+ only, and only with your own `fm_helper`.

## License

MIT — see [LICENSE](LICENSE).
