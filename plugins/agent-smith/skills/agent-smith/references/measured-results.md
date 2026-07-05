# Measured results — the evidence behind the routing

Read this only when you need the WHY behind a routing rule, a tier, or a caveat.
All results from the author's eval harness (hidden-test graded, temp 0).

## Fleet tiers (agent-gym, hidden-test graded, trusted = 2 consecutive ≥90% runs)

| Model | Size | Earned tiers | Evidence |
|---|---|---|---|
| gpt-oss:20b | 12 GB | **TRUSTED**: code-gen, struct, repo edits, app-builds (first ever) | double perfect sweep 14/14 ×2 (2026-07-04); collected every other model's stable-miss scalps |
| gemma4:26b | 17 GB | TRUSTED: code-gen/struct/edits; ASSIST: app-build; vision + design crown | 12/12 then 13/14 (07-01); defended blinded design rubric 21:19.5 vs gpt-oss AND vs Agents-A1 |
| qwen3-coder:30b | 18 GB | TRUSTED: struct, edits; ASSIST: code-gen (4/5 stable), app-build (2/3) | confirmed 07-04; app_http_api miss = real multi-scenario gap |
| agents-a1 (InternScience) | 21 GB | TRUSTED everywhere — **BENCH, no lane** | double perfect sweep ×2 (07-05); ties gpt-oss at 1.75× RAM, 2–4× slower; lost design 21:19.5; decorrelated lineage → premium witness/consensus third voice |
| llama3.2:3b | 2 GB | bulk text only; RETIRED from agentic (0/5) | 07-01 baseline |
| gemini-pro (cloud) | — | quality ceiling; escalation only | 5/5 agentic but 300–640s/task + mid-loop 503s |

Scouting lesson (Qwythos vs A1, 07-05): provenance (official org, paper, license)
predicted capability; branding predicted nothing. Interview everything; trust no name.

## Design rubric history (blinded since 07-04; 6 dims ×2 pts ×2 tasks = /24)

- gemma4 21 : gpt-oss 19.5 — split: gpt-oss won config/API-design; lost rate_limiter to
  reasoning residue (dead branch w/ falsely-documented ValueError, commented-out debug
  prints, commented-out demo).
- gemma4 21 : agents-a1 19.5 — same shape: A1's config_loader most ambitious yet
  (validators, nested schemas) but its except-wrapper SWALLOWS its own ValidationError →
  failing validators silently accepted. Discipline beats features, three defenses running.
- **Watch-item for ALL reasoning-model drafts:** residue — commented-out debug prints,
  dead branches, docstring claims for code that isn't there.

## Vision (gemma4, 10-item spot eval 2026-07-04, construction-truth + own-eyes grading)

Window-sized screenshots/dialogs/charts/code renders: EXACT text fidelity 10/10 (hex codes,
prices, nav labels) → trusted for bulk triage. **Tall full-page captures (e.g. 1200×5300):
small text CONFIDENTLY FABRICATED** (invented brand "RhythmoSonic Creative", fake nav/button
labels, Test→Text) while big headings stayed exact. Mechanism: encoder downscaling. Hence
the rule: tile scrolling captures; never act on small-text claims from a tall image.
Agents-A1 vision confirmed working via ollama (n=1, accurate); full eval not yet run.

## Transcription (mlx-whisper large-v3-turbo, 6-clip construction-truth eval 2026-07-05)

~98% semantic accuracy on general + technical English; 2–4 s/clip after model load.
Digit normalization is a feature ("ninety four point five decibels" → "94.5 decibels";
"$482.17"). The two real misses in 89 words were Sanskrit terms via a synthetic voice
(Bhairavi → "Beravi", tanpura → "tampura") → verify rare proper nouns by eye.

## Break-even + verification economics (measured 2026-06-15)

Small tasks: the skill spent MORE Claude tokens than baseline (overhead dominated).
Big payloads: ~19% net savings on a 715KB report summarize. Bulk sweeps are the best
case (283-run classify sweep, 07-04). Verify large offloads by SAMPLING — a full re-read
pays for the input twice and erases the saving. Delegated code review: ~33% confident
false positives (one proposed "fix" would have regressed intended behavior) — verify
flagged functions only, trust nothing unverified.

## Witness sensor (shipped 07-05)

First live catch within minutes: llama3.2:3b answering "Google" for its own creator
(the witness model said "Alibaba Cloud"). Rationale: silent regressions
(ollama weight updates, quant changes) are invisible without sampled re-verification.
Default witness = gpt-oss:20b; agents-a1 is the premium alternative (decorrelated lineage).

## Cloud sockets

Groq VERIFIED LIVE with `openai/gpt-oss-120b` (free tier, extreme speed) via
`--backend openai --base-url groq`; needs GROQ_API_KEY; Cloudflare 403s bare urllib
(fixed: real User-Agent). hf-xet download bug workaround: `HF_HUB_DISABLE_XET=1`.
