# r/LocalLLaMA post — READY TO POST (drafted 2026-07-05, not yet posted)

Title: I built a gym where my local models earn trust with hidden tests. gpt-oss:20b swept it twice, so I gave it real work (open source)

---

A few days ago I posted about agent-smith, my plugin that offloads drafting to free and local models. Since then it grew into something I did not expect: a full trust pipeline for a local fleet, where models earn their lanes, keep earning them, and lose them when they slip. After a grounded lit sweep I am fairly confident the integrated version does not exist anywhere else yet. Receipts below, repo at the bottom, MIT.

**The core idea: models should earn trust, not be assigned it.**

I run an eval gym on my MacBook (M3 Pro, 36GB). 14 tasks with hidden pytest suites the models never see: code gen, structured extraction, repo bug fixes, and full small app builds in a sandboxed tool loop (list, read, write, run, finish). Promotion to trusted for a capability requires two consecutive clean runs. Everything at temp 0.

**gpt-oss:20b walked in cold and swept all 14 tasks. Twice.** First model to earn trusted on agentic app builds in my harness. It built a todo CLI, a CSV reporter, and a stdlib HTTP API from written specs against tests it never saw, at 12GB on a laptop. The fleet now: gpt-oss:20b for code quality, gemma4:26b for vision and design, qwen3-coder:30b for fast drafts. They miss different things, which matters later.

**Then I gave it real work, and the failure was the best part.** First production ticket: build a small stats module. It died on turn 3. My harness caps generation at 1600 tokens per turn, the model tried to write a doc heavy file in one native tool call, the call got truncated mid JSON, and Ollama returned a 500. The gym never caught this because its tasks happened to fit under the cap. One real ticket found two real bugs in an hour. Both got logged with verdicts, both got fixed, and the retry shipped a module that passed my adversarial tests.

**The gym does not care what a model is called.** This week I interviewed a trending 9B tune with a frontier model's name stitched into it (1.5M downloads, "uncensored," the works). Same conditions as everyone: hidden tests, temp 0, Q4. Result: 10/14. Respectable agentic chops for 5.6GB, but blocked tier on code gen, one shots up to 260 seconds (slower than my 20B), and nothing it passed isn't done better by something already in the fleet. No lane. The branding showed up nowhere in the results. That is the whole point of hidden tests.

**Every delegation is logged, judged, and now watched.** One JSON line per run. After review I mark it: verdict good, or verdict bad with a reason and a task tag. The report computes routing weights per task shape per model: quality rate plus a consecutive good streak. Streak of 5 means light review, streak of 10 means spot checks only, one bad resets it. And because trusted models can rot silently (Ollama weight updates, quant changes), there is now a **witness sensor**: local runs get silently re-run on a second model and compared, on a spaced schedule where the interval doubles with every clean check and collapses back on any failure. It caught something in its first two minutes of existence: llama3.2:3b confidently answering that Google created it. The witness disagreed. Llama3.2 is Meta's model.

**Disagreement fires escalation.** Batch mode can run every item through two local models at temp 0. Agreement gets accepted, disagreement writes both answers and queues the item for a stronger model or a human. Review attention lands exactly on the items most likely to be wrong instead of random spot checks.

Why bother when APIs are cheap: I could not afford my subscription tier. The fleet has absorbed 348 delegations so far (109 minutes of compute, a 283 item classify sweep, ~215K output tokens), all work that would have been metered quota. Reviewed work is running at 80% verified good, and every bad is queued to become a regression test. Local pays for itself in headroom, and nothing leaves the machine.

The part that surprised me: before writing this I ran a research sweep on the current literature. Online bandit routing is exactly where the field went in 2025 and 2026 (MixLLM, BaRP, ParetoBandit). Escalation on disagreement got formalized in two papers this spring. But a personal, local version where your own verified outcomes update routing over an Ollama fleet, failures become regression tests, and a witness process audits your trusted models? As far as I can tell, power users glue it together themselves or it does not exist. So here is my glue.

Honest caveats, because this sub deserves them:

* The streak math is simple counters, not LinUCB. It works, it is not fancy.
* Consensus and witness modes only work on short structured outputs. Two models will never string match on prose.
* The witness is a smoke detector, not a proof system.
* Small tasks are not worth delegating, the overhead eats the savings. Measured it.
* Runs landing in the same second from the same model can still collide in the ledger keying. Known, on the list.

Side note for a future post: this week the fleet also learned to hear. Local whisper transcription earned its tier the same way (98% semantic accuracy, measured), and I got Meta's sam-audio running on Apple Silicon, which its README says needs CUDA. That one took eight workarounds and deserves its own writeup.

Repo (MIT): https://github.com/negativetime/agent-smith-plugin

The scripts are pure stdlib Python and talk to Ollama directly, so they work standalone. There is also a Claude Code plugin wrapper if you use that, but nothing requires it.

---

Posting notes: US weekday morning best; day-2 crosspost to r/ollama; one-line pointer
comment on the existing r/ClaudeAI thread (post 1unnv59). Witness paragraph updated for
the FSRS schedule shipped after the original draft.
