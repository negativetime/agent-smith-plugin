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
  says "use Gemini" or "agent-smith", mentions the local fleet, asks to save Claude tokens,
  or asks where tokens go / to audit or reduce Claude token usage. Gemini drafts; Claude scopes,
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
| Info-doc HTML (the HTML-docs rule) | md-draft → styled HTML via `references/html-doc-shell.html` | the draft's content, parity check, saving |

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
Pure stdlib, runs on macOS/Linux/Windows (`python3` vs `python`; skill dir
`~/.claude/skills/agent-smith` vs `%USERPROFILE%\.claude\skills\agent-smith`).
Key: `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`) in the environment.

**`--tag SHAPE` is REQUIRED on every call** (enforced in code 2026-07-28 — an untagged call
exits 2 before spending anything). Habit alone left 863/915 runs untagged and therefore
invisible to `gap_report.py` and the hebbian router. Reuse an existing shape or coin one;
use `--tag smoke` for throwaway routing checks so they stay out of the review queue.

```bash
SKILL=~/.claude/skills/agent-smith
python3 "$SKILL/scripts/gemini.py" --tag copy-draft "Explain X in 5 bullets"
cat spec.txt | python3 "$SKILL/scripts/gemini.py" --tag draft-spec "Make a checklist"
python3 "$SKILL/scripts/gemini.py" --search --tag research "What's new in Swift?"
python3 "$SKILL/scripts/gemini.py" --file report.pdf --tag long-digest "Summarize as bullets"
python3 "$SKILL/scripts/gemini.py" --file inv.pdf --schema s.json --tag classify "Extract items"

# BATCH (local, zero Claude tokens per item): manifest = one path per line;
# images ride vision, text appends to prompt; per-item .out.txt + ONE JSON summary.
python3 "$SKILL/scripts/gemini.py" --backend ollama --batch files.txt --tag classify --out-dir out "Classify: ..."

# CONSENSUS batch (disagreement fires escalation): each item on TWO local models at
# temp 0; agree -> accept; disagree -> .A/.B files + _escalate.txt queue. SHORT outputs only.
python3 "$SKILL/scripts/gemini.py" --backend ollama --model llama3.2:3b \
  --batch records.txt --consensus gpt-oss:20b --tag classify --out-dir cls "One word: ..."
```

Windows: same flags, `python` launcher, `Get-Content` for stdin.
**Flags:** `--tag` (REQUIRED) · `--model` · `--system` · `--file` (repeatable) · `--search` ·
`--json`/`--schema` · `--temperature` · `--max-tokens` · `--thinking-budget N` ·
`--preflight` (gemini-cli syntax check) · `--list-models` · `--no-tailor` (below).
API internals: [references/gemini-api.md](references/gemini-api.md).

**Per-model prompt tailoring (2026-07-26).** `--system` carries TASK framing (role/style);
`gemini.py` separately auto-appends a MODEL framing clause — a known, measured failure mode
for whichever model is actually resolved (e.g. gpt-oss:20b → strip reasoning-residue dead
code before returning; qwen3-vl:4b → flag long digit strings as unverified unless reading a
crop). Every call through a profiled model gets the fix instead of relying on Claude to
retype a lane note by hand. Table = `MODEL_PROFILES` in `gemini.py` (evidence-only: add an
entry after a `verdict.py bad`, not a hunch). `--no-tailor` opts a single call out; a
`[tailor]` stderr line + `"tailored"` ledger field confirm it fired. `ROUTE_WARNINGS` (not
suppressed by `--no-tailor`) separately flags a known-bad model+tag combo already called out
as a Route BLOCK below — a routing decision, not something a prompt clause should paper
over. Design + verification: [references/model-tailoring-2026-07-26.md](references/model-tailoring-2026-07-26.md).

## Backends (`--backend`) — default `gemini`, reach for edges

| backend | runs on | cost | files/web | use when |
|---|---|---|---|---|
| `gemini` | Google cloud (API key) | free tier, rate-limited | **yes** | anything substantial; ONLY one with `--file`/`--search` |
| `gemini-cli` | your OAuth login | **PAID — user's $20/mo Google AI Pro** | no | THE lane when the free API is slow/429ing (measured 2026-07-12: 3.8s vs 50s+ congested API). Text-only: pipe file contents via stdin. Use what's paid for |
| `fm` | this Mac (~3B) | free | no | private + simple bulk (`FM_HELPER` path) |
| `ollama` | this Mac | free, unlimited | images yes | private/offline/high-volume; the FLEET below |
| `openai` | any OpenAI-compatible URL | free tiers exist | no | burst beyond Gemini; shorthands `groq`\|`openrouter`\|`openai`\|`ollama`; auth `OPENAI_API_KEY` (Groq: `GROQ_API_KEY`). Groq `openai/gpt-oss-120b` = verified free frontier-adjacent. **Free clouds may train on your data — private work stays local** |

## Local fleet routing (gym-earned; evidence → [references/measured-results.md](references/measured-results.md))

- **Quality code / app builds:** `gpt-oss:20b` (12 GB) — TRUSTED code-gen/struct/edits/
  app-builds (double perfect sweep). Review watch-item: reasoning residue (commented-out
  debug prints, dead branches, doc claims for absent code).
- **Vision + design:** `gemma4:26b` (17 GB) — auto-picked when images present. Design-crown
  holder (discipline: claims match code). **Vision rule: tile tall scrolling captures —
  small text on full-page images gets confidently invented.**
- **Vision pre-screen — TRUSTED (2026-07-12, 2-consecutive gate):** `qwen3-vl:4b` (3.3 GB
  dl, ~8 GB loaded, 256k ctx) — co-resides with gpt-oss:20b. Run 1: **9/9** (incl. tiny-text
  OCR on a dense SC editor); run 2: **8/9 on fresh corpus incl. a TALL 5265px scroll** where
  it read even small-text prices correctly (no gemma4-style invention). The one miss —
  dropped a leading digit in a 10-digit app ID on a full-window shot — read EXACTLY on a
  field crop. **Lane rule: for exact long digit strings (IDs, serials, keys), crop the field
  first or double-read.** Use for "which screen / did the dialog open / read this field":
  `--backend ollama --model qwen3-vl:4b --file shot.png`. gemma4:26b keeps the
  quality/design crown.
- **Fast bulk drafts:** `qwen3-coder:30b` (18 GB) — ollama default; 2–8s one-shots.
- **Long private digests — NEW LANE (validated 2026-07-12):** `gpt-oss:20b` at up to
  **131k context** — RAM stays flat at 12 GB (MXFP4 MoE), 3/3 needle recall + correct
  comprehension measured at 52k tokens (~4.6 min). `gemini.py` now auto-sizes `num_ctx`
  from input length (Ollama silently truncates otherwise). Use for transcripts/contracts/
  logs too private for free cloud tiers; beyond ~130k tokens split it or use `--backend
  gemini` (1M). Caveat: the witness re-run doubles the cost of a long-prompt call.
- **Lighter backup:** `qwen2.5-coder-smith:14b` (9 GB, our gym-gated fine-tune).
- **Bench / second opinion:** `agents-a1` (21 GB, trusted everywhere, no lane) — decorrelated
  lineage; premium consensus/witness third voice. `llama3.2:3b` = tiny text floor only.
- **Cloud model choice:** `flash` for bulk text; **`pro` for code/design/research synthesis**
  and as escalation when local attempts fail.
- **Residency (36 GB Mac, since the claude-mem observer went local 2026-07-12):** the observer
  keeps `gpt-oss:20b` (12 GB) hot most of the day, and it + `qwen3-coder:30b` (18 GB) can't
  co-reside in GPU memory — routine one-shots on the 26b/30b now pay a 20–60s swap and evict
  the observer's model. Prefer `--model gpt-oss:20b` for routine local drafts; reach for
  gemma4:26b (vision) / qwen3-coder:30b deliberately and expect the swap. If Ollama wedges
  (model stuck "Stopping...", requests hang): `kill` the `llama-server` runner PID, or
  restart Ollama.app.
- Always: **the model drafts, you verify** — every winner has shipped a bug a review caught.

## DEFAULT-TO-LOCAL routes — gap report 2026-07-25

**Enforced in code as of 2026-07-27 (cost pass — user wants minimum spend):** `gemini.py`'s
`DEFAULT_LOCAL_FOR_TAG` table now auto-routes `--tag doc-format|classify|code-draft|
vision-prescreen|long-digest|subagent-fanout` to the free local model below whenever BOTH
`--backend` and `--model` are left unset — logs `[cost] --tag X defaults to local`. Passing
either flag explicitly still wins (e.g. `--backend gemini` forces cloud). `--search` always
forces cloud (no local web grounding) regardless of tag. Bare `gemini.py "…" --tag SHAPE`
now costs $0 for these six shapes without having to remember `--backend ollama` by hand —
reach for the tag alone. Tags below this line without a `DEFAULT_LOCAL_FOR_TAG` entry
(`translate`, `copy-draft`, `design`, `research`, …) still default to paid cloud — their
local evidence isn't strong enough yet to force it silently.

Reach for these WITHOUT re-deriving the gap report; each is a measured gap joined with a
ledger-trusted (or trial-ready) route. Tag every run so the streak builds.

- **Read-only fan-out / grep-and-summarize sweeps** → `--backend ollama --batch`
  (`--tag subagent-fanout` — must match `gap_report.py`'s `SHAPES` tag exactly, or the run
  is invisible to the gap report; `fanout-digest` was a stale/wrong tag name here until
  2026-07-27, never actually used). Biggest untouched vein: 663 Claude-side Agent/Explore
  calls, **0 delegated**. No verdicts yet — trial route; verdict the first few to build
  the record.
- **Long-document digest** (logs, transcripts, CSVs, contracts) → `--backend ollama
  --model gpt-oss:20b` (131k ctx; 1g/0b) (`--tag long-digest`). ~141M Claude context tokens
  went to reading long docs; default local first, Claude reads the distilled version.
- **Screenshot / vision prescreen** → `--backend ollama --model qwen3-vl:4b --file shot.png`
  (`--tag vision-prescreen`; 3g/0b TRUSTED). ~127M Claude context tokens of screenshots;
  local first-pass describes, Claude views only flagged shots. Digit-string crop rule applies.
- **Mechanical code boilerplate from a clear spec** (Codable conformance, enum plumbing,
  UI-label tables, test scaffolds) → `qwen3-coder:30b` or `gemma4:26b` (`--tag code-draft`).
  Gym-trusted shape; expect the residency swap (see fleet routing above).
- **Classification / tag / label batches** → `llama3.2:3b` (`--tag classify`; 1g/0b),
  `--consensus gpt-oss:20b` when accuracy matters.
- **Web research** → `--search --tag research`. `gemini.py` now defaults `--tag research`
  to **pro** automatically when `--model` is omitted (fixed 2026-07-27 — was silently
  defaulting to flash, which scored 0g/1b, MISROUTED; pro scores 9g/1b). Passing
  `--model flash` still works but logs a route warning. The remaining gap is volume, not
  model choice: 2% delegated as of 07-25 — the blocker is habit, not tooling.
- **Route BLOCK:** `doc-format @ gpt-oss:20b` is 0g/**3b** — do not send doc-format there
  until it passes an agent-gym task. Use gemini-pro (5-streak, LIGHT REVIEW — first earned
  tier), or gemma4:26b / qwen3-coder:30b (2g/0b each).

## Standing offload targets — token audit 2026-07-12

Measured: 136M Claude output tokens/30d, only 0.2% offloaded. Biggest single consumer
(claude-mem observer, ~16% of ALL output) now runs on local `gpt-oss:20b` via claude-mem's
openrouter provider → `http://127.0.0.1:11434/v1`. Ranked remaining targets — main-loop web
research (habit gap: 679 searches + 701 fetches ran on Opus), read-only subagent fan-outs,
first-draft code via smith_agent, screenshot pre-screening, session hygiene — with numbers,
the claude-mem config/revert path, and the generalizable "point any custom-endpoint tool at
the fleet" precedent: [references/token-audit-2026-07-12.md](references/token-audit-2026-07-12.md).
Re-run the audit: `python3 /Users/joshualangberg/Python/docs/token_audit.py`.

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
yourself**, then verdict it. Canonical source + harness: `~/Python/agent-gym/`.

## Local transcription — transcribe.py (audio → text, free, private)

`python3 "$SKILL/scripts/transcribe.py" FILE` (wav/mp3/m4a/aiff; `--timestamps`;
`--model small` for speed). mlx-whisper on Apple Silicon, ~2–4s per clip after load,
~98% semantic accuracy (measured). **Caveat: verify rare proper nouns by eye** (Sanskrit/
domain terms can be misheard). Pattern: transcribe locally, then offload the text digest.

## Progress tracking — ledger, verdicts, routing weights, witness

- Every run appends one JSON line to `data/usage.jsonl` (fail-safe; `SMITH_LEDGER` overrides).
  Review: `python3 "$SKILL/scripts/usage_report.py"` (`--today`, `--last N`, `--unreviewed`).
- **Say what a run is FOR (2026-07-18).** The ledger used to record only size/speed/model, so
  887 runs were indistinguishable after the fact and nothing could be reviewed or routed on
  purpose. Records now carry `purpose` (auto-derived from the prompt), `tag`, `project`, and
  input filenames. **Pass `--tag SHAPE` on every delegation** (`research`, `doc-format`,
  `classify`, `vision`, `copy-draft`, `long-digest`…) — it groups the report and feeds the
  hebbian routing weights below; add `--purpose "…"` when the first prompt line makes a poor
  title. `SMITH_LOG_PROMPTS=0` records only explicit purposes. `usage_report.py --unreviewed`
  is the review queue — coverage was ~4% (799/887 unverdicted), so the routing weights rest on
  a thin sample; verdict a few whenever you're already in the ledger.
- **Verdicts:** `ok` = completed, not correct. After review:
  `verdict.py good|bad|stale "why" [--tag TASKTYPE] [--model M] [--script S]`. Tags feed the
  report's **hebbian routing weights**: per (task-shape, model) quality + streak → review
  tier (≥5 light review, ≥10 spot-check). Good strengthens a route; one bad resets it.
  `stale` = the output no longer exists to judge (pre-archive run, deleted workdir); it clears
  the queue WITHOUT counting as good or bad.
- **Outputs are archived (2026-07-28).** Every run writes its answer to
  `data/outputs/<date>/<ts>-<script>-<model>.txt` and the ledger row carries `output_file`.
  Before this, the loop demanded a verdict on every run while the tool stored nothing to
  judge — 17 runs aged into permanently ungradeable. `SMITH_NO_ARCHIVE=1` opts out.
- **The WebSearch nudge (2026-07-28).** `scripts/research_nudge.py` is a non-blocking
  `PreToolUse` hook on `WebSearch` (wired in `~/.claude/settings.json`). It stays quiet for
  the first 2 searches of a session, then prints the exact `--search --tag research` command
  on the 3rd and every 4th after. Tunable via `SMITH_NUDGE_AFTER` / `SMITH_NUDGE_EVERY`.
  Verified live: `hookSpecificOutput.additionalContext` DOES reach Claude on `PreToolUse`
  (the docs are ambiguous; this was tested, not assumed) and the search still runs.
- **Feed failures back:** every `bad` = a ready-made regression test → new agent-gym task
  before that shape is delegated again.
- **Find the gaps — `python3 "$SKILL/scripts/gap_report.py"`.** The ledger only sees work that
  WAS delegated; it is structurally blind to work Claude did itself, which is where the gaps
  are. This joins the ledger against `token_audit.json` (mined from Claude transcripts) and
  splits the result two ways: **UNUSED** (Claude did it while a trusted route sat idle) and
  **MISROUTED** (delegated to a model the ledger scores badly at that shape). Standing
  measurement (unchanged 07-19 → 07-25): **web research 2% delegated** — 1,467 Claude calls
  vs 31 fleet runs while `research @ gemini-pro` sits at 9g/1b; read-only subagent fan-out
  0% of 663 Agent spawns. Ranked routes now live in "DEFAULT-TO-LOCAL routes" above. Run it monthly, on gym day, or whenever quota gets tight;
  `--refresh` re-mines the transcripts first. Caveat it prints itself: tags only exist on runs
  since 2026-07-18, so pre-tag history is recovered by per-shape heuristics and ratios read as
  a floor.
- **Fleet identity check:** `ollama pull` updates weights IN PLACE — a trusted model can
  silently become a different model. `python3 "$SKILL/scripts/fleet_check.py"` compares
  current digests vs the accepted baseline (`--accept` after a deliberate update + re-gate);
  run it when anything smells off, and always after pulling updates.
- **Witness comparison is STRUCTURAL, not textual.** Python uses the stdlib `ast`; other
  languages use tree-sitter when installed — `pip install tree-sitter tree-sitter-language-pack`
  (OPTIONAL: absent, non-Python falls back to exact-match, the pre-2026-07-20 behaviour, and
  nothing raises). Without it the sensor could not tell benign formatting from real drift in
  Swift/TS/YAML — spacing, semicolons and comments all read as disagreement, which is what
  pinned it at 18% agreement. Operators and keywords are kept in the fingerprint (they are
  anonymous tree-sitter nodes and were briefly dropped, making `a + b` equal `a - b`);
  delimiters are excluded.
- **Self-healing tool calls:** if Ollama's server-side tool parser 500s mid-session
  (truncated/complex calls — the known killer), smith_agent now drops native tool schemas
  and continues the SAME session via the JSON-fallback protocol instead of dying.
- **Ollama-down failover:** the whole local fleet also runs through any OpenAI-compatible
  server via `--backend openai --base-url` (llama-server, `mlx_lm server` — proven, LM
  Studio). Ollama is the hub, not a dependency. LM Studio pilot (install, CLI, perf vs
  Ollama, a real tool-loop bug found + fixed): [references/lmstudio-pilot-2026-07-15.md](references/lmstudio-pilot-2026-07-15.md).
- **Witness drift sensor (trust has a forgetting curve):** local runs are silently re-run
  on `SMITH_WITNESS_MODEL` (default gpt-oss:20b) on an FSRS-style schedule — the interval
  grows with each consecutive agreement (every 4 runs -> 8 -> 16 ... cap 256) and COLLAPSES
  back to 4 on any disagreement or verified-bad verdict. Verification never reaches zero;
  stable routes just earn longer intervals. Disagreement = DRIFT SIGNAL in the report
  (never an auto-verdict). `SMITH_WITNESS_RATE` switches back to flat-rate sampling.
  **Comparator fixed 2026-07-18:** it was exact-match on raw text, applied to one-shot CODE
  generation — a markdown fence or a type hint counted as drift, so the sensor sat at 18%
  agreement, every model's streak stayed pinned at 0, the interval never grew, and the
  logged DRIFT entries before this date are mostly spurious (ignore them). Now: fences are
  stripped, and when both outputs parse as Python they're compared as AST skeletons with
  annotations/docstrings/comments normalized away — so formatting differences agree while
  a real logic change, a different algorithm, or a differing classification still flags.

## Troubleshooting

- `GEMINI_API_KEY is not set` → user exports it, fresh shell.
- HTTP 429 → auto-retries; persistent → switch flash↔pro or `gemini-cli`.
- Model not found → `--list-models`. Weak output → tighten prompt, lower temp, escalate pro.
- Blocked/empty → block reason is on stderr; rephrase.
