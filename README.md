# agent-smith

a claude code skill that offloads the bulky half of a task to cheaper models, then makes claude verify the result before it counts.

claude scopes, reviews, integrates and delivers. gemini, a paid glm plan, or a local ollama fleet do the drafting, digesting and research. every run is logged, every reviewed run gets a verdict, and the verdicts decide which model gets which kind of work next time. trust is measured in a gym, never asserted.

## why

claude tokens are the expensive, rate-limited resource. most of what burns them is not judgment, it is volume: reading a 200 page pdf, drafting boilerplate from a clear spec, running a web search and summarizing ten sources, classifying a thousand records. those jobs are checkable. a cheaper model can draft them and claude can spot-check the output for less than it costs to do the work itself.

the skill exists to make that the default rather than something you remember to do.

## what it does

- `gemini.py` sends a prompt (plus optional files, images, web grounding, json schema) to one of several backends and prints the answer on stdout, metadata on stderr
- `smith_agent.py` runs a sandboxed tool loop for multi-step scratch builds: fix a bug, add a feature, build a small app in a throwaway directory
- `transcribe.py` transcribes audio locally with mlx-whisper (apple silicon)
- `embed.py` does embeddings and reranking via cloudflare workers ai
- a usage ledger records every run with its task shape, model, purpose and archived output
- `verdict.py` records good/bad after review; the ledger turns verdicts into per-shape routing weights
- `gap_report.py` joins the ledger against your claude transcripts to show where claude did work a trusted route could have done
- `fleet_check.py` catches an `ollama pull` silently replacing a model you had earned trust in
- two non-blocking `PreToolUse` hooks nudge claude toward the fleet at the moment it reaches for `WebSearch` or a read-only `Agent` fan-out

## install

```bash
git clone https://github.com/negativetime/agent-smith-plugin.git ~/.claude/skills/agent-smith
export GEMINI_API_KEY=...   # in your shell profile, never in a file in the repo
```

pure stdlib python 3. no pip install needed for the core. optional extras:

- `ollama` for the local backend
- `mlx-whisper` for `transcribe.py`
- `tree-sitter` + `tree-sitter-language-pack` for structural witness comparison in non-python languages (without it those fall back to exact match)

claude code picks the skill up from `SKILL.md` automatically. the description there is written so it triggers proactively on delegable work even when nobody says "gemini".

## quick start

```bash
SKILL=~/.claude/skills/agent-smith

# plain draft
python3 "$SKILL/scripts/gemini.py" --tag copy-draft "explain X in 5 bullets"

# stdin as context
cat spec.txt | python3 "$SKILL/scripts/gemini.py" --tag draft-spec "make a checklist"

# web grounded research with sources
python3 "$SKILL/scripts/gemini.py" --search --tag research "what changed in swift 6.3"

# digest a big file so claude does not have to read it
python3 "$SKILL/scripts/gemini.py" --file report.pdf --tag long-digest "summarize as bullets"

# structured extraction
python3 "$SKILL/scripts/gemini.py" --file invoice.pdf --schema items.json --tag classify "extract line items"

# local batch, one file per manifest line, zero cloud calls
python3 "$SKILL/scripts/gemini.py" --backend ollama --batch files.txt --tag classify --out-dir out "classify: ..."
```

then review the output and close the loop:

```bash
python3 "$SKILL/scripts/verdict.py" good --tag research --model pro
python3 "$SKILL/scripts/verdict.py" bad "invented an api that does not exist" --tag code-draft --model gpt-oss:20b
```

`--tag` is required. an untagged call exits 2 before spending anything. that is deliberate: without tags the ledger could not group runs by shape, so nothing could be reviewed or routed on purpose. use `--tag smoke` for throwaway checks so they stay out of the review queue.

## the loop

1. scope a tight, self-contained prompt. the backend has none of your conversation.
2. delegate with a tag.
3. review in proportion to the payload. backends hallucinate apis and citations; run or lint code. never re-read a large input to verify, sample it.
4. integrate yourself. send focused revision prompts instead of redoing the work.
5. record a verdict.

a `good` verdict strengthens a (shape, model) route. a `bad` one resets its streak and should become a regression task in the gym before that shape is delegated to that model again. at a streak of 5 the route earns light review, at 10 spot-check.

## backends

| backend | where | cost | files / web |
|---|---|---|---|
| `gemini` | google api key | free tier, rate limited | yes, the only one with `--file` and `--search` on pdfs |
| `gemini-cli` | your google oauth login | whatever plan you pay for | text only, pipe via stdin |
| `ollama` | your machine | free, private | images yes |
| `fm` | apple foundation models on device | free, private | no |
| `openai` | any openai compatible url | varies | depends on host |

`--backend openai --base-url` takes shorthands: `groq`, `openrouter`, `openai`, `ollama`, `zai`, `cloudflare`. auth resolves per host from `GROQ_API_KEY`, `ZAI_API_KEY`, `CF_API_TOKEN` (+ `CF_ACCOUNT_ID`), else `OPENAI_API_KEY`.

free cloud tiers may train on your data. anything private stays on `ollama` or `fm`.

## routing

some tags route themselves when you leave `--backend` and `--model` unset:

- `DEFAULT_LOCAL_FOR_TAG`: `doc-format`, `classify`, `vision-prescreen`, `subagent-fanout` go to a free local model
- `DEFAULT_PAID_FOR_TAG`: `code-draft`, `long-digest` go to a flat-rate subscription where the marginal call costs nothing
- `--search` always forces cloud
- `research` defaults to gemini pro, because flash measured badly on it

explicit flags always win. the tables live in `scripts/gemini.py` and only change on evidence from the ledger or the gym, not on a hunch.

`gemini.py` also appends a per-model framing clause to the system prompt for models with a known, measured failure mode (`MODEL_PROFILES`). `--no-tailor` opts a single call out.

the exact fleet and lanes in `SKILL.md` are the author's, earned on the author's hardware. treat them as a worked example. your fleet has to earn its own tiers.

## scripts

| script | what |
|---|---|
| `gemini.py` | the main helper. backends, files, search, schemas, batch, consensus, tagging, archiving |
| `smith_agent.py` | sandboxed agentic loop. scratch dirs only, it executes model shell |
| `transcribe.py` | local audio to text |
| `embed.py` | embeddings and reranking on workers ai |
| `usage_report.py` | ledger aggregates, routing weights, `--unreviewed` review queue |
| `verdict.py` | `good` / `bad` / `stale` after review |
| `verdict_db.py` | sqlite mirror of the verdict trail for non-python consumers |
| `gap_report.py` | unused and misrouted capacity, ledger joined against claude transcripts |
| `fleet_check.py` | model digests vs an accepted baseline |
| `research_nudge.py`, `fanout_nudge.py` | non-blocking `PreToolUse` hooks |
| `test_preflight.py` | tests for the `--preflight` syntax check |

## layout

```
SKILL.md            what claude reads. routing, backends, lanes, the loop
PROJECT.html        canonical status doc
scripts/            everything above
references/         playbooks, api notes, measured results, model tailoring, the html doc shell
evals/              research grounding eval and its fixtures
data/               fleet_ids.json is tracked. usage.jsonl, verdicts.db, outputs/ are not
docs/               drafts
SECURITY.md         how to report a vulnerability
```

## environment

| var | purpose |
|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | gemini backend |
| `OPENAI_API_KEY`, `GROQ_API_KEY`, `ZAI_API_KEY`, `CF_API_TOKEN`, `CF_ACCOUNT_ID` | openai compatible hosts |
| `FM_HELPER` | path to the foundation models helper binary |
| `SMITH_LEDGER` | override the ledger path |
| `SMITH_LOG_PROMPTS=0` | record only explicit `--purpose`, not prompt text |
| `SMITH_NO_ARCHIVE=1` | do not archive outputs |
| `SMITH_NO_FALLBACK=1` | do not reroute research when gemini hits a monthly cap |
| `SMITH_NUDGE_AFTER`, `SMITH_NUDGE_EVERY` | hook cadence |

keys live in the environment. nothing in this repo reads them from a file.

## things learned the hard way

- a ledger that records size and speed but not purpose is unreviewable. 887 runs were indistinguishable after the fact.
- demanding a verdict on every run while storing no output to judge produces runs that age into ungradeable. outputs are archived now.
- a headline metric nobody trusts is worse than none. "858 unreviewed" was really 58 once legacy rows and smoke tests were separated.
- documenting a gap does not close it. web research sat at 2 percent delegated for weeks with the route sitting trusted and idle. the hooks exist because the decision moment is when claude reaches for the tool, not when it reads a doc.
- `ollama pull` replaces weights in place under the same tag. a trusted model can become a different model overnight.
- a tiny local vision model beat two remote ones outright on ocr of a real screenshot. bigger was not better; measuring was.
