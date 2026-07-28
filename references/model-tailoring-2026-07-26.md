# Per-model prompt tailoring — 2026-07-26

Josh's idea: instead of sending the same generic prompt to whichever model a task
gets routed to, have Claude tailor the request per model — the output should be
better if the prompt accounts for that specific model's known behavior.

## Why this was a real gap, not just a nice-to-have

`SKILL.md` and `measured-results.md` already carried plenty of model-specific
failure modes (gpt-oss:20b's reasoning residue, gemma4:26b's tall-screenshot
invention, qwen3-vl:4b's dropped digits on full-window shots) — but only as prose
Claude was supposed to remember and manually retype into `--system` on every
delegation. `--system` in practice was only ever used for TASK framing ("Conversion
copywriter + front-end dev"), never for correcting a model's own defects. That's
exactly the kind of thing that silently gets skipped under time pressure.

## What shipped

`scripts/gemini.py` gained a `tailor_system(system, model, tag, no_tailor)`
function, wired into every call path that actually reaches a model: oneshot
(fm/ollama/gemini-cli/openai/gemini backends), `--batch`, and `--consensus` (primary
and MODEL2 tailored independently, since they're different models with different
profiles).

- **`MODEL_PROFILES`** (dict, model name -> one-sentence corrective clause,
  auto-appended to `--system`): seeded from already-measured failure modes —
  gpt-oss:20b (strip reasoning residue), gemma4:26b (don't invent small text on
  tall screenshots), qwen3-vl:4b (flag long digit strings as unverified without a
  crop), agents-a1 (don't let except-blocks swallow real errors).
- **`ROUTE_WARNINGS`** (dict, (model, tag) -> stderr warning): known-bad combos
  already called out as routing blocks in `SKILL.md` (gpt-oss:20b + doc-format,
  flash + research). Deliberately NOT folded into a prompt clause — "use a
  different model" is a routing decision, not something a system-prompt caveat
  should paper over. Fires even under `--no-tailor`.
- `--no-tailor` opts a single call out of the auto-appended clause (route warnings
  still print). A `[tailor] appended corrective clause for <model>` stderr line and
  a `"tailored"` (`"tailored_consensus"` in batch mode) field in the ledger record
  make it visible after the fact — feeds future `gap_report.py`-style analysis of
  whether tailoring actually moves the good/bad ratio for a given model.

## Verification performed

- Unit-level: `tailor_system` checked in isolation for (a) profiled model + no
  existing `--system` text, (b) profiled model + existing text (clause appended,
  not replacing), (c) unprofiled model (no-op, identical string, empty key list),
  (d) `--no-tailor` suppressing a profiled model's clause, (e) `ROUTE_WARNINGS`
  firing on stderr for a known-bad combo, (f) route warning still firing under
  `--no-tailor`.
- Live: real Ollama calls through `gemini.py --backend ollama`.
  - `--model gpt-oss:20b` (profiled) → `[tailor]` line printed, call succeeded,
    ledger row recorded `"tailored": ["gpt-oss:20b"]`.
  - `--model qwen3-coder:30b` (unprofiled, control) → no `[tailor]` line, ledger
    unaffected — confirms the no-op path is truly silent.
  - `--model gpt-oss:20b --no-tailor` → no `[tailor]` line despite the profile
    existing.
  - Smoke-test rows scrubbed from `data/usage.jsonl` afterward (filtered by
    `tag: smoke-test`) so they don't skew `usage_report.py`/`gap_report.py` stats.
- Confirmed `usage_report.py` and `gap_report.py` still run cleanly with the new
  `tailored`/`tailored_consensus` ledger fields present (both are additive, read
  via `.get()` elsewhere, nothing broke).

## Extending it

Add a `MODEL_PROFILES` entry only after a `verdict.py bad` traces back to a
specific, repeatable model defect — not speculatively. Keep each clause to one
sentence: extra instruction text competes for attention inside a 3–20B model's
context window and can backfire on the small/fast ones (the whole reason the
existing lane notes stayed terse). `ROUTE_WARNINGS` entries come from the same
evidence trail as a `SKILL.md` "Route BLOCK" note — write the block there first,
mirror it here as a runtime guard.
