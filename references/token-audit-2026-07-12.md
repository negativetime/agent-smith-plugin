# Token audit 2026-07-12 — where Claude tokens actually go, and what to offload

Measured from ALL `~/.claude/projects` transcripts (2,860 sessions, 1.9 GB; effectively
30 days of usage). Re-run anytime: `python3 /Users/joshualangberg/Python/docs/token_audit.py`.
Full styled report: `/Users/joshualangberg/Python/docs/token-audit-2026-07-12.html`.

## Headline numbers (30 days)

| metric | value |
|---|---|
| Output tokens | **136M** (146M all-time) |
| Cache-write tokens | 1.22B |
| Cache-read tokens | **27.2B** (context re-carried every turn — the volume driver) |
| Offload ratio before audit | **0.2%** (257k Gemini out vs 136M Claude out — the skill was barely denting the whale) |
| Daily output run-rate | 3–12M tokens |

## Output tokens by consumer

| consumer | out tokens | share | note |
|---|---|---|---|
| Opus 4.8 main loop (interactive app-building) | 101.7M | ~70% | Bibliome iOS, Teletype MIDI, Prism, Refill, More Garlic, Birdsong marathons |
| **claude-mem observer (Haiku)** | 23.0M | **~16%** | 31.9k background observation generations — **moved to local fleet 2026-07-12** |
| Fable 5 main loop | 17.9M | ~12% | |
| Sonnet 5/4.6 (subagents, light sessions) | 3.7M | ~3% | |
| — of which subagent sidechains | 12.8M | ~9% | 700 Agent spawns, 439 general-purpose |

Context stuffing: Read text 524M chars (.swift 37.5M / 6,622 reads), Read images 459M
b64 chars (1,522 reads — App Store screenshots + QA captures, some multi-MB PNGs read
repeatedly), Bash 26M, web results ~3.2M (679 WebSearch + 701 WebFetch on the MAIN loop).

## The precedent: claude-mem observer now rides the fleet

claude-mem's `openrouter` provider accepts a custom base URL → pointed at Ollama.
`~/.claude-mem/settings.json`: `CLAUDE_MEM_PROVIDER=openrouter`,
`CLAUDE_MEM_OPENROUTER_BASE_URL=http://127.0.0.1:11434/v1`, API key `ollama` (dummy),
model `gpt-oss:20b` (MoE speed — dense 26b would cost ~10 GPU-h/day at ~1,060 obs/day),
`CLAUDE_MEM_API_TIMEOUT_MS=120000` (cold loads abort at the 30s default).
Saves ~22.6M out + 2.5B cache-read tokens/mo of subscription quota. Verified live same day.

- Known-benign: `Empty OpenRouter init response` log errors — the observer prompt's
  skip_guidance permits empty output; gpt-oss obeys literally on the no-tool-yet init
  turn (Haiku used to ack chattily). Init context stays in history; observations store fine.
- Quality fallbacks: model→`gemma4:26b` (slower/better) or `CLAUDE_MEM_PROVIDER=gemini`
  (flash-lite, off-GPU). Revert: `settings.json.bak-20260712` + kill worker PID.
- **Generalize this:** any Claude-adjacent tool with an OpenAI-compatible/custom-endpoint
  option can ride the fleet the same way. Check for a provider/base-URL setting BEFORE
  accepting that a background service burns subscription quota.

### Operational findings from day one (2026-07-12, same-day)

- **Residency contention (36 GB Mac):** observer keeps gpt-oss:20b (12 GB) hot; it and
  qwen3-coder:30b (18 GB) / gemma4:26b (17 GB) can't co-reside in GPU memory. Routine local
  drafts should use `gpt-oss:20b` too (one hot model serves observer + drafts); the big
  models are deliberate escalations that pay a 20–60s swap.
- **Ollama wedge mode:** repeated client-side aborts (claude-mem's request timeout during
  cold loads) can leave the model stuck "Stopping..." — API answers but generations hang.
  Recovery: `kill` the `llama-server` runner PID (from `pgrep -fl llama-server`), or restart
  Ollama.app. The 120s `CLAUDE_MEM_API_TIMEOUT_MS` reduces the abort rate.
- **gemini.py stdin bug (FIXED same day):** piped stdin was silently DROPPED whenever a
  prompt argument was also given — every backend, including the documented
  `cat spec.txt | gemini.py "prompt"` shape. Now stdin appends below the prompt arg.
  Symptom to recognize: absurdly small `prompt=` token count in the meta line + generic
  invented output.

## Ranked standing offload targets (impact × gym trust)

1. ~~claude-mem observer~~ → **DONE** (above). −22.6M out/mo.
2. **Web research & doc digestion in the main loop** (~5–8M out/mo): 679 searches +
   701 fetches ran on Opus (silvermakersmarks ×110 for Hallmarked, Apple docs ×68,
   tarot content). Route to `gemini.py --search` / `--file` — research @ gemini-pro is
   already at *light review* (streak 6). The gap is habit, not tooling.
3. **Read-only subagent fan-outs** (~6M of 12.8M sidechain): "search/summarize X"
   general-purpose Agent spawns → `gemini.py` or `--backend ollama --batch`. Keep
   code-editing subagents on Claude.
4. **First-draft modules / ports / test suites** (~10–15M out/mo slice of the Opus loop):
   `smith_agent.py --model gpt-oss:20b` in a SCRATCH dir from a ticket-style spec;
   Claude reviews + integrates.
5. **Screenshot pre-screening** (1,522 image reads/mo): fleet vision (gemma4:26b)
   answers "did the dialog open / which screen is this?" during QA/automation loops;
   Claude only eyeballs final or ambiguous shots. Downscale first: `sips -Z 1440`.
6. **Session hygiene** (not offload; biggest cache lever): 6k–15k-message marathons
   drive most of the 27.2B cache reads. Fresh session per feature — claude-mem restores
   context, and its observations are now free.

Stays on Claude (standing rules): live-repo edits, correctness-critical debugging,
security-sensitive work, credentials/execution, final review of every fleet draft.

## Fleet-gap assessment (2026-07-12, same-day follow-up)

Measured context ceilings: the whole fleet was SERVED at 32k while models natively support
gpt-oss:20b **131k** · gemma4:26b **262k** (vision) · qwen3-coder:30b **262k** ·
qwen2.5-coder-smith:14b 32k (architectural ceiling) · llama3.2:3b 131k.

**Gaps FILLED same day:**
1. **Long private digest lane** — gpt-oss:20b at 131k ctx costs no extra RAM (12 GB flat,
   MXFP4 MoE); 3/3 needle recall + comprehension at 52k tokens in 4.6 min. gemini.py now
   auto-sizes `num_ctx` (was silently truncating at server default — two bugs fixed:
   missing num_ctx entirely, then 4-chars/token underestimate vs measured ~3.2–3.4).
   Verdict logged good @ long-digest.
2. **(from earlier)** doc-format HTML-ification, observer offload.

**Gap FILLED (2026-07-12 afternoon): resident vision.** `qwen3-vl:4b` pulled (3.3 GB dl,
~7.9 GB loaded). Gate run 1: **9/9** on a verified 3-screenshot corpus (Bibliome home +
tools, dense SoundCheck editor) — badges, sidebar lists, pipeline arrows, and tiny-text OCR
(sequence dropdown, Key field) all exact; UI-truncated text reported as-visible, not
invented. Verdict logged good @ vision-prescreen. **Run 2 on fresh screenshots still needed
for TRUSTED** (2-consecutive gym rule). Alternates if it ever regresses: `qwen3-vl:8b`
(6.1 GB), `minicpm-v4.6` (1.6 GB).

Ollama runtime state: `OLLAMA_MAX_LOADED_MODELS=2` set (launchctl + a LaunchAgent that
persists it at login). `OLLAMA_KEEP_ALIVE=30m` set for the CURRENT session only — extending
the LaunchAgent to persist it was denied pending explicit user approval (login-persistence
mechanism). The instant-unload pathology ("Stopping..." after every request, cold-load on
every observer call) cleared after the app restart; true 2-model simultaneous residency
under memory pressure is NOT yet verified — gpt-oss was still evicted in the one dual-load
test. Watch `ollama ps`.

**Remaining candidate: qwen2.5-coder-smith:14b is the weakest slot** — hard 32k ctx
ceiling, borderline agentic (3/5), 9 GB disk; superseded by gpt-oss:20b for every lane
except consensus diversity. USER-GATED (custom fine-tune): retire only on explicit request.

**NOT fillable locally (stay cloud/Claude):** web-grounded research (needs --search),
DESIGN synthesis (gym: gemini-pro is the measured ceiling), correctness-critical/security/
execution (policy + structural).

**Witness caveat:** the drift sensor re-runs primaries on a 17 GB swap model — on long-
context calls that doubles a 4-minute job and evicts the resident model. Consider a
size-aware witness skip (e.g. sample long-prompt runs less often) if long digests become
routine.
