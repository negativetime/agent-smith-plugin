# Measured results — the evidence behind the routing

Read this only when you need the WHY behind a routing rule, a tier, or a caveat.
Canonical harness + full verdicts: `~/Python/agent-gym/` (`BASELINE-2026-07-01.md`).

## Fleet tiers (agent-gym, hidden-test graded, trusted = 2 consecutive ≥90% runs)

| Model | Size | Earned tiers | Evidence |
|---|---|---|---|
| gpt-oss:20b | 12 GB | **TRUSTED**: code-gen, struct, repo edits, app-builds (first ever) | double perfect sweep 14/14 ×2 (2026-07-04); collected every other model's stable-miss scalps |
| ~~gemma4:26b~~ **REMOVED 2026-08-16** | 17 GB | was: TRUSTED code-gen/struct/edits; ASSIST app-build; vision + design crown | 12/12 then 13/14 (07-01); defended blinded design rubric 21:19.5 vs gpt-oss AND vs Agents-A1. **Deleted after 4 weeks unused + vision-v1 measured it inventing 4 fields on a tall page. DESIGN LANE NOW VACANT.** Re-pull + re-gate if needed |
| qwen3-coder:30b | 18 GB | TRUSTED: struct, edits; ASSIST: code-gen (4/5 stable), app-build (2/3) | confirmed 07-04; app_http_api miss = real multi-scenario gap |
| qwen2.5-coder-smith:14b | 9 GB | gate-passed lighter backup | our LoRA fine-tune; app-build holdout 8/14 vs base 3/14 |
| agents-a1 (InternScience) | 21 GB | TRUSTED everywhere — **BENCH, no lane** | double perfect sweep ×2 (07-05); ties gpt-oss at 1.75× RAM, 2–4× slower; lost design 21:19.5; decorrelated lineage → premium witness/consensus third voice |
| llama3.2:3b | 2 GB | bulk text only; RETIRED from agentic (0/5) | 07-01 baseline |
| gemini-pro (cloud) | — | quality ceiling; escalation only | 5/5 agentic but 300–640s/task + mid-loop 503s |

Scouting lesson (Qwythos vs A1, 07-05): provenance (official org, paper, license)
predicted capability; branding predicted nothing. Interview everything; trust no name.

**SCOUTED AND REJECTED — do not re-scout without a new reason:**
`qwen3.8:27b` (2026-08-16, 17 GB, Apache 2.0, dense, 262K ctx, vision+tools+thinking).
L3 agentic 6/6 and L0/L2 13/16 — genuinely strong — but **TRANSLATE 2/5** (fails the three
tasks built from the real More Garlic i18n bugs), **132s median / 736–797s on design+docs**
vs qwen3-coder:30b's 2–8s, and **4.91x output bloat on design+docs** (the axis gemma4 wins
the blinded rubric on) despite 0 residue on short code tasks. Ties a 3.3 GB model on vision.
Cannot co-reside with claude-mem's gpt-oss:20b. **No lane earned.** Untested: its 262K ctx.

## Design rubric history (blinded since 07-04; 6 dims ×2 pts ×2 tasks = /24)

- gemma4 21 : gpt-oss 19.5 — split: gpt-oss won config/API-design; lost rate_limiter to
  reasoning residue (dead branch w/ falsely-documented ValueError, commented-out debug
  prints, commented-out demo).
- gemma4 21 : agents-a1 19.5 — same shape: A1's config_loader most ambitious yet
  (validators, nested schemas) but its except-wrapper SWALLOWS its own ValidationError →
  failing validators silently accepted. Discipline beats features, three defenses running.
- **2026-08-16 (4-way, blinded via `agent-gym/blind_design.py`, run `20260816-151357`) —
  re-gate after gemma4's removal left the lane vacant:**

  | model | config_loader | rate_limiter | /24 |
  |---|---|---|---|
  | gemini-pro | 11.0 | 11.5 | **22.5** |
  | **gpt-oss:20b** | **11.5** | 8.5 | **20.0** ← best LOCAL, takes the lane |
  | qwen3-coder:30b | 6.0 | 9.0 | **15.0** — do NOT route design here |
  | gemini-flash | HTTP 503 | 11.5 | incomplete |

  ★ **gpt-oss:20b reproduced its OWN 07-04 defect exactly**: rate_limiter is again
  *int-only `consume` on a float bucket*, six weeks and a fresh draw apart. Not flakiness —
  a stable model characteristic. Its config_loader remains excellent (11.5: separable
  validator, unknown-key detection, `from exc` chaining), so the July "won config, lost
  rate_limiter" split is now a REPLICATED result, not a one-off.
  ★ qwen3-coder:30b showed the other half of the July signature: dead `elif` branch its own
  comment calls unreachable, docstring claiming a `json.JSONDecodeError` it converts away,
  commented-out demo, and a printed "Valid configuration processed successfully" for work it
  never did. **Weak at design; it holds the SPEED lane, not this one.**
  ⚠ **flash TIED pro (11.5) on the only task both finished** — its other run died to a
  transient 503, which is infra, not quality. So there is **NO measured basis for a
  `DEFAULT_MODEL_BY_TAG["design"] = "pro"` entry**, and none was added. An earlier
  recommendation of "use gemini-pro for design" was an assertion this run did not support.
  n=2 tasks, single draw — directional only.
- **Watch-item for ALL reasoning-model drafts:** residue — commented-out debug prints,
  dead branches, docstring claims for code that isn't there.

## Vision (gemma4, 10-item spot eval 2026-07-04, construction-truth + own-eyes grading)

Window-sized screenshots/dialogs/charts/code renders: EXACT text fidelity 10/10 (hex codes,
prices, nav labels) → trusted for bulk triage. **Tall full-page captures (e.g. 1200×5300):
small text CONFIDENTLY FABRICATED** (invented brand "RhythmoSonic Creative", fake nav/button
labels, Test→Text) while big headings stayed exact. Mechanism: encoder downscaling. Hence
the rule: tile scrolling captures; never act on small-text claims from a tall image.
Agents-A1 vision confirmed working via ollama (n=1, accurate); full eval not yet run.

## Vision REPLICATED + extended (vision-v1 doc suite, 2026-08-16)

First repeatable vision harness: `~/Python/agent-gym/vision/` (corpus builder, runner with
`--repeat`, self-tested grader, every reply stored for offline re-grading). Construction
truth — invented authors/DOI/figures, so nothing scores from world knowledge. 4 items:
clean page, 1275×5625 tall capture, degraded scan, dense numeric table. 24 runs.

**Scored on TWO axes, because they fail separately.** Recall alone ranks a confident liar
first, so `invented` (answered confidently and WRONG) is tracked apart from `declined`
(answered null — a safe failure a caller can fall back on).

| model | size | recall | invented | avg |
|---|---|---|---|---|
| qwen3.8:27b | 17 GB | 100% (12/12) | 0 | 54s |
| **qwen3-vl:4b** | **3.3 GB** | **100%*** | 0* | 53s |
| gemma4:26b | 17 GB | 80% | 4 | 10s |

\* 99% / 1 invented until diagnosed: it emitted JSON number `1.884` where the page prints
`1.8840`. Adding "every value MUST be a JSON string" fixed it — it read the digits
correctly all along. **Bare JSON numbers silently destroy significant figures; demand
string-typed values in any extraction prompt.** Model-independent bug.

**⚠ The 2026-07-04 gemma4 tall-page fabrication REPRODUCED on a corpus it has never seen.**
On the tall capture it scored 20%, INVENTED 4 author names ("Eric Ronning" for
"Ilse Kavanagh-Ruiz") and declined 4 more — in **6 seconds**, vs 57s for a model that
actually read the page. The speed is the tell: it isn't reading, and it doesn't say so.
So gemma4's "vision crown" above must be read as **window-sized captures ONLY** — it is
not safe for tall or small-text document pages, and the tiling rule is mandatory, not
advisory.

**qwen3-vl:4b ties a model 5× its size** on document pages, including 7.5pt body text on a
5625px-tall image and every minus sign/trailing zero in a numeric table. It also co-resides
with claude-mem's gpt-oss:20b on a 36 GB Mac where a 17 GB model cannot. No reason to
route document vision anywhere else.

Caveat: 4 synthetic items, one document family — no handwriting, multi-column, non-Latin
script, or real scanned PDFs. Enough to disqualify gemma4 for tall pages; not a
certification.

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
(witness qwen2.5-coder-smith said "Alibaba Cloud"). Rationale: silent regressions
(ollama weight updates, quant changes) are invisible without sampled re-verification.
Default witness = gpt-oss:20b; agents-a1 is the premium alternative (decorrelated lineage).

## Cloud sockets

Groq VERIFIED LIVE with `openai/gpt-oss-120b` (free tier, extreme speed) via
`--backend openai --base-url groq`; needs GROQ_API_KEY; Cloudflare 403s bare urllib
(fixed: real User-Agent). hf-xet download bug workaround: `HF_HUB_DISABLE_XET=1`.
