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
  social or marketing posts; or any mass first-draft code or boilerplate. Also trigger when
  the user says "use Gemini" or asks to save Claude tokens. Gemini drafts; Claude scopes,
  cross-checks, integrates, and delivers. Do NOT use for short/quick/interactive work, small
  edits, correctness-critical debugging, or security-sensitive tasks — and never for the
  EXECUTION half of a task: deploying to Cloudflare, posting to a live account, committing or
  building, or anything needing your credentials, tools, or live repo context. Gemini has no
  access to your accounts or tools, so those stay on Claude or a script you control. Don't use
  it to install or configure Gemini itself.
---

# Gemini Offload

## The deal

You (Claude) are the orchestrator and the quality bar. Gemini is a fast, large-context
workhorse you can shell out to for generation that would otherwise burn Claude tokens.
The pattern is simple: **delegate the bulk, keep the judgment.** Gemini drafts and digests;
you scope, review, verify, and finish.

Gemini's output is an **intermediate, never a deliverable.** Don't paste it to the user raw.
Pull every result back through your own review before it counts as done. This skill exists to
spend Gemini's tokens instead of Claude's — which directly serves the user's standing
preference to be economical with Claude quota.

## What to offload vs. what to keep

**Send to Gemini** (voluminous and checkable):
- **Research / fact-finding** — it has live Google Search grounding (`--search`), so it's
  good for fresh facts, gathering sources, "what is the current state of X".
- **Digesting large inputs** — summarize, extract, classify, or transform big PDFs,
  transcripts, logs, CSVs, datasets. Let Gemini ingest the raw file (`--file`) and hand
  back a digest, instead of reading 200 pages into *your* context.
- **First-draft generation** — boilerplate code, config files, test scaffolds, regex, a
  rough function from a clear spec.

**Keep on Claude** (subtle, stateful, or expensive to get wrong):
- Orchestration and deciding what to offload.
- Repo-aware edits and anything needing the local filesystem, tools, or build.
- Correctness-critical reasoning, security, tricky debugging.
- Final review, integration, and verification of Gemini's output.

**Rule of thumb:** if the work is *voluminous and checkable*, send it to Gemini. If it's
*subtle, stateful, or expensive to get wrong*, keep it.

**Break-even — don't offload trivially small work.** This skill has fixed overhead: you read
this file, run the helper, and pull Gemini's output back into your context to review it. For a
quick task — a short factual answer, a tiny file, a few-line snippet — that overhead costs
*more* than just doing it yourself, in both tokens and wall-clock. The win only materializes
when the offloaded payload is genuinely big. Rough line: offload when the input is more than
~50 KB / a few thousand words, or the generation is more than a page or two; below that, just
do it directly. (Benchmarked: on small tasks the skill spent *more* Claude tokens than the
baseline — the overhead dominated.)

## Task playbooks — recurring workflows

Several common, token-heavy workflows decompose the same way: **Gemini drafts the words; Claude
(or a script with your credentials) does the action.** Offload the *generation/research* half;
keep the *execution* half, because Gemini can't touch your accounts, tools, or repo.

| Workflow | Offload to Gemini (the bulk) | Keep on Claude / a script |
|---|---|---|
| **Planning** | Research, first-draft plans, option write-ups | The decision, repo-aware specifics, the committed plan |
| **Website content** | Copy, blog/FAQ, headlines, first-draft HTML/CSS, meta/alt text | Wiring into the repo/build, voice/legal pass, in-browser test |
| **Cloudflare / infra** | Draft `wrangler.toml`, Worker scaffolds, Dockerfiles, CI YAML *as text* | The deploy/config itself — **Claude's MCP + your creds, never Gemini** |
| **Business postings** | One announcement → many platform-specific posts, captions, hashtags | The actual posting — a script with **your** tokens; final approval |

The rule for every row: **offload the words, keep the action.** Anything that deploys, commits,
posts, charges, or runs against a live service stays with you. For the full recipe of each —
including the exact helper commands — read [references/playbooks.md](references/playbooks.md).

## The loop

1. **Scope.** Write a tight, self-contained prompt. Gemini has **none** of this
   conversation's context — spell out the goal, the format you want, and any constraints.
2. **Delegate.** Call the helper (below). Pick the model by difficulty (see Model choice).
3. **Review critically — but in proportion to the payload.** Gemini hallucinates, may invent
   APIs or citations, lacks your repo context, and tends to be verbose. Verify factual claims
   (spot-check or re-ground), run or lint any code, and strip the fluff. Treat it as a
   smart-but-unsupervised draft. **Crucially: do not re-ingest a large input just to check the
   output.** If you offload a 200-page PDF and then read all 200 pages yourself to verify, you
   paid for it twice and erased the entire saving. For big payloads, verify by *sampling* —
   confirm the format, check a few known anchors, spot-check a couple of sections — and trust
   the rest. Full re-reading to verify is only acceptable when the input was small to begin
   with (in which case, see the break-even note below — you probably shouldn't have offloaded).
4. **Finish.** Integrate into files yourself, do the correctness-critical parts, polish. If
   the draft is close but off, send Gemini a focused revision prompt rather than redoing it.
5. **Report.** Tell the user what you delegated and that you verified it.

## Using the helper

Path: `scripts/gemini.py` under this skill dir. The **answer prints to stdout**; **model + token
usage + grounding sources print to stderr** — read both.

The script is pure-stdlib Python and runs identically on **macOS, Linux, and Windows** — it just
makes HTTPS calls to the Gemini API. Only the launcher and paths differ by OS; adapt to whatever
the current platform uses:

- **Python launcher:** `python3` on macOS/Linux; `python` (or `py -3`) on Windows.
- **Locating the script:** when this is installed as a **plugin**, Claude Code sets
  `$CLAUDE_PLUGIN_ROOT` to the plugin root, so the helper is at
  `$CLAUDE_PLUGIN_ROOT/skills/agent-smith/scripts/gemini.py`. As a **personal skill** it's under
  `~/.claude/skills/agent-smith` (macOS/Linux) or `%USERPROFILE%\.claude\skills\agent-smith`
  (Windows). The snippets below resolve either case automatically.
- **API key:** the script reads `GEMINI_API_KEY` (fallback `GOOGLE_API_KEY`) from the environment.
  If it isn't set on this machine, set it first — `export GEMINI_API_KEY=...` (macOS/Linux) or
  `setx GEMINI_API_KEY "..."` (Windows; open a new shell afterward).

**macOS / Linux (bash):**

```bash
# Resolves whether installed as a plugin ($CLAUDE_PLUGIN_ROOT set) or as a personal skill:
SKILL="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-smith}"
SKILL="${SKILL:-$HOME/.claude/skills/agent-smith}"

# Plain generation (default model = flash)
python3 "$SKILL/scripts/gemini.py" "Explain X in 5 bullets"

# Long prompt via stdin
cat big_spec.txt | python3 "$SKILL/scripts/gemini.py" --model pro "Turn this into a checklist"

# Web-grounded research (returns source URLs on stderr)
python3 "$SKILL/scripts/gemini.py" --search "What's new in the latest Swift release?"

# Digest a large file (PDF/CSV/txt/image) — Gemini ingests it, not you
python3 "$SKILL/scripts/gemini.py" --file report.pdf "Summarize the findings as bullets"

# Structured extraction (force JSON, optionally against a schema)
python3 "$SKILL/scripts/gemini.py" --file invoices.pdf --schema schema.json "Extract line items"

# Code draft with a role
python3 "$SKILL/scripts/gemini.py" --model pro --system "Senior Python engineer" \
  "Write a function that parses ISO-8601 durations into seconds"
```

**Windows (PowerShell)** — same flags, just the `python` launcher and the Windows path; pipe stdin
with `Get-Content`:

```powershell
$SKILL = if ($env:CLAUDE_PLUGIN_ROOT) { "$env:CLAUDE_PLUGIN_ROOT\skills\agent-smith" } else { "$env:USERPROFILE\.claude\skills\agent-smith" }

python "$SKILL\scripts\gemini.py" "Explain X in 5 bullets"
Get-Content big_spec.txt | python "$SKILL\scripts\gemini.py" --model pro "Turn this into a checklist"
python "$SKILL\scripts\gemini.py" --search "What's new in the latest Swift release?"
python "$SKILL\scripts\gemini.py" --file report.pdf "Summarize the findings as bullets"
```

**Flags (identical on every OS):** `--model` (flash|pro|flash-lite|full-name) · `--system` ·
`--file PATH` (repeatable) · `--search` (Google grounding) · `--json` / `--schema PATH` ·
`--temperature` · `--max-tokens` · `--thinking-budget N` (0 = off, faster/cheaper on Flash) ·
`--preflight` (gemini-cli: syntax-check generated code and auto-retry once on a SyntaxError) ·
`--list-models`. The `gemini-cli` backend also auto-applies a deny-all-tools policy
(`scripts/deny_all_tools.toml`) so it can only return text — never edits files — and runs ~25% leaner.

Deeper API details, the full model list, grounding/Files-API internals, and a curl fallback
are in [references/gemini-api.md](references/gemini-api.md). Read it only if you hit something
the flags above don't cover.

## Backends — where the work actually runs (`--backend`)

All four backends offload the heavy lifting *off Claude* (that's the whole point — spare Claude's
tokens). They differ in cost, power, and privacy. **Default to `gemini`**; reach for the others
when their specific edge matters.

| `--backend` | Runs on | Cost | Power | Files / web? | Use when |
|---|---|---|---|---|---|
| `gemini` *(default)* | Google cloud (API key) | free tier, **rate-limited** | highest (frontier 3.x) | **yes** — `--file`, `--search`, JSON schema | anything substantial; the **only** one that ingests PDFs/images or does live web research |
| `gemini-cli` | Google cloud (your OAuth login) | your subscription/account quota, **no API rate limit** | highest (same Gemini models) | no (text only) | same Gemini quality but **free-tier 429s are throttling you** — runs on the quota you already have via the CLI login |
| `fm` | this Mac (Apple Intelligence) | free, **no quota** | small (~3B) | no (text only) | data must stay **private/offline**; quick simple bulk you don't want to spend Gemini quota on |
| `ollama` | this Mac (local model) | free, **no quota** | mid (model-dependent) | no (text only) | offline/private with better quality than `fm`; **unlimited** high-volume bulk with no rate limits |

- **Gemini stays the brain.** The API (`gemini`) is the most capable and the only one that ingests
  files or researches the web. Use the others for *text-in → text-out* work you already hold —
  summarize, rewrite, classify, draft boilerplate — especially when it's **private, offline, or so
  high-volume that Gemini's free-tier rate limits would throttle you** (the local ones have no 429s).
- `gemini-cli` drives the locally-installed **Gemini CLI on your OAuth/Google login**, so it runs on
  your subscription/account quota instead of the metered API key — the way to keep using top Gemini
  models when free-tier 429s bite. **One-time setup:** run `gemini` once and choose *"Login with
  Google."* The skill hides `GEMINI_API_KEY` from the CLI so it uses that login; set
  `GEMINI_CLI_USE_API_KEY=1` to use the key instead. Text-only; it auto-applies a deny-all-tools
  policy so it can only generate (never edits files) and runs ~25% leaner.
- `fm` (Apple Foundation Models, macOS 26+ with Apple Intelligence) is **opt-in and not bundled** —
  this repo ships no binary on purpose (don't run opaque executables from strangers). It needs an
  `fm_helper` you supply; point the skill at it with `FM_HELPER=/path/to/fm_helper`. If unset, the
  `fm` backend just errors and you stay on `gemini`/`ollama`. (README has notes on building one.)
- `ollama` needs `ollama serve` running and a model pulled. **First-time local setup is disk-aware:**
  run `bash "$SKILL/scripts/setup_local_model.sh"` — it reads your free disk and offers a model tier
  sized to it (qwen3-coder:30b ~18 GB / qwen2.5-coder:14b ~9 GB / 7B ~5 GB), then pulls your pick. The
  default model is `qwen3-coder:30b` (the bake-off's best local coder — a 30B MoE, 3B active, so fast);
  lighter options `--model qwen2.5-coder:14b` or `--model llama3.2:3b`. **If the user wants the local
  backend and no coder model is installed, offer this tiered choice — sized to their actual `df` free
  space — before pulling; don't assume a size.**

**No Gemini account? You don't need one.** If `GEMINI_API_KEY` isn't set, don't dead-end — run fully
local with no account via `--backend ollama`. Offer **Gemma** (Google's open model: `gemma3:12b` /
`gemma3:27b`) for general text, or **qwen2.5-coder** for code. Point the user at
`scripts/setup_local_model.sh` to pick one sized to their disk. (Getting a free key at
https://aistudio.google.com/apikey is still the faster, stronger path — mention it — but it's optional.)

```bash
python3 "$SKILL/scripts/gemini.py" --backend fm "Rewrite this paragraph more concisely: ..."
python3 "$SKILL/scripts/gemini.py" --backend ollama "Draft a Python function that ...: ..."
```

## Model choice (auto-tier)

- **`flash`** (default, `gemini-flash-latest`): bulk summarization, extraction, classification,
  simple drafts. Fast and cheap — your default for high-volume work.
- **`pro`** (`gemini-pro-latest`): hard reasoning, nuanced research synthesis, tricky code.
  Reach for it when Flash's quality isn't enough.

Start on Flash; **escalate to Pro only if the Flash output is weak.** Don't default to Pro —
that spends the user's Gemini quota faster for no benefit on easy tasks.

**For code, default to Pro, not Flash.** A coding bake-off across six free backends (correctness
*and* a judged design rubric) found `gemini-pro` the clear winner on both — 5/5 on hidden-test
tasks and the only model that got concurrency *and* validation right in the design task. Flash and
the local models each dropped tasks or shipped subtler design flaws. So for **code generation,
design, refactors, and bug-review, use `--model pro`**; Flash stays the default for bulk *text*.

**Best offline/local coder = `--backend ollama --model qwen3-coder:30b`** (the default). In the
bake-off it tied `qwen2.5-coder:14b` on correctness (4/5) but ran **~2× faster** (a 30B MoE with only
3B active) and clearly beat it on design — real encapsulation, `time.monotonic()`, and fuller
validation where 14b was terser and used wall-clock. The trade-off is disk: 18 GB vs 9 GB. For a
lighter footprint, `qwen2.5-coder:14b` is the solid runner-up. Use local when the work must stay
private/offline or run unlimited — accepting it's below Pro, so verify with extra care. Full results:
the bake-off `FINDINGS.md` in the skill's workspace. And regardless of model: **the model drafts,
you verify** — every backend
in the bake-off, the winner included, shipped at least one bug a review had to catch.

## Token economy

The point is to move the *big raw inputs and the bulky generation* onto Gemini, while keeping
only the *result* in your context. So: let Gemini ingest the giant PDF and return a 1-page
digest; don't read the PDF yourself first. But never skip the review step — an unverified
Gemini answer that's wrong costs more (in your time and the user's trust) than it saved.

## Troubleshooting

- **`GEMINI_API_KEY is not set`** → ask the user to set it (`export GEMINI_API_KEY=…` on
  macOS/Linux, `setx GEMINI_API_KEY "…"` on Windows), then retry in a fresh shell.
- **HTTP 429 (rate limit)** → the script auto-retries with backoff; if it persists, wait a
  beat or switch flash↔pro (separate quota buckets).
- **Model not found** → run `--list-models` to see what this key can use.
- **Weak / garbled output** → tighten the prompt, lower `--temperature`, or escalate to `pro`.
- **Blocked prompt / empty response** → the script reports the block reason on stderr; rephrase.
