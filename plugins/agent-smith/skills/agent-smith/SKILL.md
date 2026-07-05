---
name: agent-smith
description: >-
  Offload bulky or research-heavy text work to Google Gemini (via GEMINI_API_KEY), then
  verify and finish with Claude to spare Claude's tokens and context. Trigger PROACTIVELY,
  even if Gemini isn't named, whenever the heavy part of a task is generating or digesting
  text: web research on current facts or "what's new/changed in X" with source links;
  summarizing, digesting, or extracting from one long document OR many files (PDFs,
  transcripts, interviews, logs, CSVs, contracts) into bullets, tables, quotes, or themes;
  classifying or transforming many records; drafting a plan, proposal, roadmap, or research
  write-up; website, landing-page, or marketing copy (blog, FAQ, headlines, first-draft
  HTML/CSS); config or infrastructure boilerplate as text (wrangler.toml, Worker scaffolds,
  Dockerfiles, CI YAML, IaC modules); turning one announcement into many platform-specific
  social or marketing posts; or any mass first-draft code or boilerplate. Trigger for CODE
  work too: drafting a new module, class, CLI tool, or test suite from a clear spec; porting
  or translating code; or a multi-step scratch-sandbox build (fix-a-bug, add-a-feature,
  build-a-small-app) via the bundled smith_agent.py tool loop. Also trigger when the user
  says "use Gemini" or "agent-smith", mentions the local fleet, or asks to save Claude tokens. Gemini drafts; Claude scopes,
  cross-checks, integrates, and delivers. Do NOT use for short/quick/interactive work, small
  edits, correctness-critical debugging, or security-sensitive tasks — and never for the
  EXECUTION half of a task: deploying to Cloudflare, posting to a live account, committing or
  building, or anything needing your credentials, tools, or live repo context. Gemini has no
  access to your accounts or tools, so those stay on Claude or a script you control. Don't use
  it to install or configure Gemini itself.
---

# Gemini Offload

## The deal

You (Claude) are the orchestrator and the quality bar. **Delegate the bulk, keep the
judgment.** Backend output is an intermediate, never a deliverable — pull every result
through your own review before it counts. This spends the fleet's tokens instead of
Claude's, per the user's standing quota-economy preference.

## What to offload vs. keep

**Offload** (voluminous and checkable): research/fact-finding (`--search`), digesting big
inputs (`--file` — let the backend read the 200 pages, not you), first-draft generation
(code, configs, tests, copy from a clear spec).
**Keep** (subtle, stateful, expensive to get wrong): orchestration, repo-aware edits,
correctness-critical reasoning, security, final review + integration.
**Break-even:** don't offload small work — overhead exceeds savings below ~50 KB input /
a page or two of output (measured; see [references/measured-results.md](references/measured-results.md)).

## Task playbooks

**Offload the words, keep the action** — anything that deploys, commits, posts, or charges
stays with you (backends have no credentials).

| Workflow | Offload | Keep |
|---|---|---|
| Planning | research, draft plans, option write-ups | the decision, repo specifics |
| Website content | copy, blog/FAQ, first-draft HTML/CSS | wiring, voice/legal pass, browser test |
| Cloudflare/infra | wrangler.toml, Dockerfiles, CI YAML *as text* | the deploy — your MCP + creds |
| Business postings | announcement → per-platform posts | actual posting; final approval |

Full recipes: [references/playbooks.md](references/playbooks.md).

## The loop

1. **Scope** — tight, self-contained prompt; the backend has none of this conversation.
2. **Delegate** — pick backend + model below.
3. **Review in proportion to payload** — backends hallucinate APIs/citations; run/lint code.
   **Never re-ingest a large input to verify** — sample: check format, a few known anchors,
   spot-check sections. (Full re-read = you paid twice.)
4. **Finish** — integrate yourself; send focused revision prompts rather than redoing.
5. **Report + record the verdict** — tell the user what you delegated and verified, then:
   `python3 "$SKILL/scripts/verdict.py" good` (or `bad "why"`). One line, every review.

## Using the helper

`scripts/gemini.py` — answer on stdout; model/tokens/sources on stderr (read both).
Pure stdlib, runs on macOS/Linux/Windows (`python3` vs `python`). As a PLUGIN the
helper lives under `$CLAUDE_PLUGIN_ROOT/skills/agent-smith`; as a personal skill under
`~/.claude/skills/agent-smith` — the snippet below resolves both.
Key: `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`) in the environment.

```bash
SKILL="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-smith}"
SKILL="${SKILL:-$HOME/.claude/skills/agent-smith}"
python3 "$SKILL/scripts/gemini.py" "Explain X in 5 bullets"                       # flash default
cat spec.txt | python3 "$SKILL/scripts/gemini.py" --model pro "Make a checklist"  # stdin
python3 "$SKILL/scripts/gemini.py" --search "What's new in Swift?"                # grounded research
python3 "$SKILL/scripts/gemini.py" --file report.pdf "Summarize as bullets"       # file ingest
python3 "$SKILL/scripts/gemini.py" --file inv.pdf --schema s.json "Extract items" # JSON extraction

# BATCH (local, zero Claude tokens per item): manifest = one path per line;
# images ride vision, text appends to prompt; per-item .out.txt + ONE JSON summary.
python3 "$SKILL/scripts/gemini.py" --backend ollama --batch files.txt --out-dir out "Classify: ..."

# CONSENSUS batch (disagreement fires escalation): each item on TWO local models at
# temp 0; agree -> accept; disagree -> .A/.B files + _escalate.txt queue. SHORT outputs only.
python3 "$SKILL/scripts/gemini.py" --backend ollama --model llama3.2:3b \
  --batch records.txt --consensus qwen2.5-coder:14b --out-dir cls "One word: ..."
```

Windows: same flags, `python` launcher, `Get-Content` for stdin.
**Flags:** `--model` · `--system` · `--file` (repeatable) · `--search` · `--json`/`--schema` ·
`--temperature` · `--max-tokens` · `--thinking-budget N` · `--preflight` (gemini-cli syntax
check) · `--list-models`. API internals: [references/gemini-api.md](references/gemini-api.md).

## Backends (`--backend`) — default `gemini`, reach for edges

| backend | runs on | cost | files/web | use when |
|---|---|---|---|---|
| `gemini` | Google cloud (API key) | free tier, rate-limited | **yes** | anything substantial; ONLY one with `--file`/`--search` |
| `gemini-cli` | your OAuth login | subscription quota, no API limit | no | free-tier 429s throttling you (one-time: `gemini` → Login with Google) |
| `fm` | this Mac (~3B) | free | no | private + simple bulk. NOT bundled — supply your own `fm_helper` (`FM_HELPER` path); errors if unset |
| `ollama` | this Mac | free, unlimited | images yes | private/offline/high-volume; the FLEET below |
| `openai` | any OpenAI-compatible URL | free tiers exist | no | burst beyond Gemini; shorthands `groq`\|`openrouter`\|`openai`\|`ollama`; auth `OPENAI_API_KEY` (Groq: `GROQ_API_KEY`). Groq `openai/gpt-oss-120b` = verified free frontier-adjacent. **Free clouds may train on your data — private work stays local** |

## Local fleet routing (gym-earned; evidence → [references/measured-results.md](references/measured-results.md))

- **Quality code / app builds:** `gpt-oss:20b` (12 GB) — TRUSTED code-gen/struct/edits/
  app-builds (double perfect sweep). Review watch-item: reasoning residue (commented-out
  debug prints, dead branches, doc claims for absent code).
- **Vision + design:** `gemma4:26b` (17 GB) — auto-picked when images present. Design-crown
  holder (discipline: claims match code). **Vision rule: tile tall scrolling captures —
  small text on full-page images gets confidently invented.**
- **Fast bulk drafts:** `qwen3-coder:30b` (18 GB) — ollama default; 2–8s one-shots.
- **Lighter backup:** `qwen2.5-coder:14b` (9 GB). First-time setup: `bash "$SKILL/scripts/setup_local_model.sh"` — disk-aware, offers a tier sized to free space. No Gemini account needed for local.
- **Bench / second opinion:** `agents-a1` (21 GB, trusted everywhere, no lane) — decorrelated
  lineage; premium consensus/witness third voice. `llama3.2:3b` = tiny text floor only.
- **Cloud model choice:** `flash` for bulk text; **`pro` for code/design/research synthesis**
  and as escalation when local attempts fail.
- Always: **the model drafts, you verify** — every winner has shipped a bug a review caught.

## Agentic offload — smith_agent.py (sandboxed tool loop)

Multi-step repo tasks (fix a bug, add a feature, build a small app):

```bash
python3 "$SKILL/scripts/smith_agent.py" --model gpt-oss:20b \
  --workdir /path/to/SCRATCH --prompt-file task.txt
# big single-file writes: add --max-gen-tokens 4096 (default 1600 truncates them)
# cloud escalation: --backend gemini --model pro (5x slower, 503 risk)
```

Rules: SCRATCH dirs only (it executes model shell — never a live repo); write the task like
a ticket (spec, exact outputs, how to verify); seed a `test_public.py`; **verify the result
yourself**, then verdict it.

## Local transcription — transcribe.py (audio → text, free, private)

`python3 "$SKILL/scripts/transcribe.py" FILE` (wav/mp3/m4a/aiff; `--timestamps`;
`--model small` for speed). mlx-whisper on Apple Silicon, ~2–4s per clip after load,
~98% semantic accuracy (measured). **Caveat: verify rare proper nouns by eye** (Sanskrit/
domain terms can be misheard). Pattern: transcribe locally, then offload the text digest.

## Progress tracking — ledger, verdicts, routing weights, witness

- Every run appends one JSON line to `data/usage.jsonl` (fail-safe; `SMITH_LEDGER` overrides).
  Review: `python3 "$SKILL/scripts/usage_report.py"` (`--today`, `--last N`).
- **Verdicts:** `ok` = completed, not correct. After review:
  `verdict.py good|bad "why" [--tag TASKTYPE] [--model M] [--script S]`. Tags feed the
  report's **hebbian routing weights**: per (task-shape, model) quality + streak → review
  tier (≥5 light review, ≥10 spot-check). Good strengthens a route; one bad resets it.
- **Feed failures back:** every `bad` = a ready-made regression test — build one before
  that shape is delegated again.
- **Witness drift sensor:** `SMITH_WITNESS_RATE` (default 5%) of local runs silently re-run
  on `SMITH_WITNESS_MODEL` (default gpt-oss:20b) and compared; disagreement = DRIFT SIGNAL
  in the report (never an auto-verdict). Verification never reaches zero.

## Troubleshooting

- `GEMINI_API_KEY is not set` → user exports it, fresh shell.
- HTTP 429 → auto-retries; persistent → switch flash↔pro or `gemini-cli`.
- Model not found → `--list-models`. Weak output → tighten prompt, lower temp, escalate pro.
- Blocked/empty → block reason is on stderr; rephrase.
