# LM Studio pilot — 2026-07-15

Evaluated LM Studio as an augment/failover to the Ollama fleet (not a replacement).
Motivation: Ollama wedge mode, tool-parser 500s, and 20–60s GPU-residency swaps
between gpt-oss:20b / qwen3-coder:30b / gemma4:26b on the 36 GB Mac (see SKILL.md
Residency note). Installed via `brew install --cask lm-studio` (0.4.19+2, signed +
notarized, Element Labs Inc). `lms` CLI bootstraps only after the GUI app has been
opened once by a human — this tool environment cannot launch GUI apps itself
(`open -a` reports success, no process ever appears; confirmed not app-specific).

## Setup — verified working

- CLI parity with Ollama: `lms get/load/unload/ls/ps/server/daemon` all real, match
  docs. Headless daemon (`lms daemon up`, backed by `llmster`) runs independent of
  the GUI, confirmed via LM Studio's own docs (not just the model's claim).
- Model IDs are HuggingFace-repo-based, not Ollama tags: `openai/gpt-oss-20b`,
  `Qwen/Qwen3-Coder-30B-A3B-Instruct`, `google/gemma-3-27b-it`, etc. `--mlx` flag
  on `lms get` restricts to MLX builds.
- OpenAI-compat server on :1234 requires `Content-Type: application/json` (Ollama
  is lenient about this — will silently 415 a lazy curl call).
- `--backend openai --base-url` convention MISMATCH between our own scripts:
  `gemini.py` wants the URL WITH `/v1` (`http://localhost:1234/v1`); `smith_agent.py`
  wants it WITHOUT (`http://localhost:1234` — appends `/v1/chat/completions` itself).
  Mixing them up 404s into an opaque `KeyError: 'choices'`. Not worth unifying (would
  break existing call sites); just remember they differ.

## Performance — did NOT confirm the MLX hypothesis (one trial, gpt-oss-20b only)

| | Ollama (GGUF, MXFP4) | LM Studio (MLX, MXFP4-**Q8**) |
|---|---|---|
| Cold load, 32768 ctx | 20.6s | 27.8s |
| Throughput, uncontended | 31.5 tok/s | 18.9 tok/s |
| Throughput, both models loaded simultaneously | — | 5.3 tok/s (real contention, unfair to judge on) |

**Caveat, not resolved:** LM Studio's community MLX pull defaulted to `MXFP4-Q8`
(higher-precision non-expert tensors) vs Ollama's pure `MXFP4` — not apples-to-apples.
A pure-MXFP4 MLX quant might close the gap; didn't chase it further (quota economy).
**Verdict on performance: inconclusive-to-negative for this model.** Don't migrate
hot fleet models to LM Studio on this evidence — Ollama won both rounds tested.

## Tool-loop reliability — real bug found AND fixed

First real-task test (an actual Bibliome backlog item, n-0002 PII-alert UX fix) via
`smith_agent.py --backend openai` **silently no-op'd**: 2 turns, zero files written,
harness reported `finished: true` anyway. Root cause (via `--transcript`): gpt-oss-20b
emitted its own native "harmony" channel syntax
(`<|channel|>analysis to=container.exec code<|message|>...`) instead of the harness's
plain-JSON fallback protocol, because `chat_openai()` sent no tool schemas at all
(comment: "the model must speak the JSON-fallback protocol from training/nudging") —
unlike the `ollama` backend path, which does send native `tools=`. gpt-oss is
specifically trained around structured tool/channel calling and doesn't degrade
gracefully to the plain-text convention on task prompts that read as agentic.

**Fixed in `scripts/smith_agent.py` (commit 2eee6c6):**
1. `chat_openai()` now sends `tools=TOOLS` like the ollama path, returns native
   `tool_calls`, and round-trips tool results via `tool_call_id` (OpenAI's actual
   multi-turn contract) — the old JSON-fallback protocol still applies underneath
   if a model ignores the schema anyway.
2. `finished` is now gated on ≥1 successful `write_file` this session. A
   `finish`/`no_tools` stop with zero writes downgrades to `finish_no_write` /
   `no_tools_no_write` instead of reporting false-positive completion. (This bug
   isn't LM-Studio-specific — any backend could hit it; just surfaced here first.)

**Re-verified same task post-patch:** 9 turns, genuine `stop: "finish"`, both files
written, Swift test independently compiled + run outside the harness (`ALL PASSED`).

## Bottom line

LM Studio is now a **genuinely trustworthy rescue lane** for `smith_agent` — the
"Ollama-down failover" claim in SKILL.md is proven, not just documented, and the
false-positive-finish bug it exposed is fixed for every backend. Keep it installed
and configured for that. Don't promote it to primary for hot models — the
throughput case isn't there yet, and may just be a quant-selection problem, not an
MLX one.
