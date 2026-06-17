# Task playbooks

Concrete Gemini-vs-Claude splits for higher-level workflows you do a lot. The principle is
always the same:

> **Gemini drafts the bulky text artifact; Claude — or a script holding your credentials —
> does anything that touches an account, a repo, a build, or a live service.**

Gemini has no access to your tools, files, logins, or this conversation. So the *generation
and research* half offloads to it; the *execution* half (deploy, commit, post, run) never
does. When a task mixes both, split it at that seam: offload the draft, keep the action.

Each playbook below lists what fires it, the Gemini half (with a runnable helper command — set
`SKILL` to the skill dir as shown in SKILL.md), the Claude/script half, and how to verify cheaply.

---

## 1. Planning

**Fires when:** the user wants a plan, proposal, roadmap, design write-up, or an
options/trade-off analysis — anything where the heavy part is *drafting prose from research*,
not making the final call.

**Offload to Gemini:**
- Background research (use `--search` for anything time-sensitive or external).
- A structured first-draft plan from a one-paragraph goal: phases, risks, open questions.
- Option write-ups ("give me 3 approaches to X with pros/cons").

```bash
python3 "$SKILL/scripts/gemini.py" --search \
  "Draft a phased rollout plan for <goal>. Include phases, owner-less task lists, risks, and open questions. Cite any external facts."
```

**Keep on Claude:**
- The actual decision and any repo-/product-specific specifics Gemini can't know.
- Sequencing against the real codebase, dependencies, and the user's constraints.
- The committed plan (and, in plan mode, the ExitPlanMode call — that's yours).

**Verify:** sanity-check Gemini's facts and that the phases map to reality; cut filler. A plan
draft is cheap to check because *you* own the judgment regardless of what it wrote.

---

## 2. Website / landing-page content

**Fires when:** the user needs copy or first-draft markup for a site or page — marketing copy,
a blog post, FAQ, feature/benefit sections, meta descriptions, alt text, or rough HTML/CSS to
iterate on. (Think a marketing site, or an app's landing / privacy / support pages.)

**Offload to Gemini:**
- Body copy, headlines, CTAs, FAQ entries, blog drafts — in volume and variations.
- First-draft semantic HTML + CSS from a description ("a hero + 3 feature cards + footer").
- SEO bits: meta titles/descriptions, alt text for a batch of images, schema.org JSON-LD.

```bash
python3 "$SKILL/scripts/gemini.py" --model pro --system "Conversion copywriter + front-end dev" \
  "Write a landing-page hero + 3 feature cards (semantic HTML, minimal CSS) for <app>. Voice: <brand voice>. Also give 2 alt headline options."
```

**Keep on Claude:**
- Wiring the markup into the real repo/framework (Workers, the actual templates, the build).
- Brand-voice final pass, factual claims about the product, legal/privacy wording.
- Accessibility and in-browser verification (the Chrome/Playwright tools are yours, not Gemini's).

**Verify:** read the copy for voice and truth (Gemini will confidently overclaim product
features — strip anything not real); lint/preview the markup yourself before it ships.

---

## 3. Cloudflare / infrastructure artifacts

**Fires when:** the user needs *config or boilerplate as text* — `wrangler.toml`, a Worker
scaffold, a Dockerfile, CI YAML, a Terraform/IaC module first draft. The drafting is bulky and
mechanical; that's Gemini's lane.

**Offload to Gemini:**
- First-draft `wrangler.toml`, Worker entry scaffolds, route/binding boilerplate.
- CI/CD pipeline YAML, Dockerfiles, infra modules from a spec.
- Explanatory docs/READMEs for the infra.

```bash
python3 "$SKILL/scripts/gemini.py" --model pro \
  "Draft a wrangler.toml + a minimal Worker (TypeScript) that serves a static site with one /api/health route. I'll fill in real account IDs and deploy."
```

**Keep on Claude — this is the hard boundary:**
- **The actual deploy/config is NEVER Gemini's.** Deploying a Worker, editing KV/R2/D1, reading
  account state — that's done through **Claude's Cloudflare MCP tools with your credentials**, or
  `wrangler` you run. Gemini cannot touch your account and must not be asked to.
- Real account IDs, secrets, bindings, and the post-deploy smoke test.

**Verify:** confirm the boilerplate matches current Cloudflare schema (it drifts — cross-check
against `wrangler` or the Cloudflare docs MCP), then *you* deploy and confirm it's live.

---

## 4. Business postings (one announcement → many posts)

**Fires when:** the user has news (app launch, update, blog post) and wants it turned into many
platform-specific posts. This is Gemini's sweet spot: high-volume, templated, low-stakes text.

**Offload to Gemini:**
- One source announcement → tailored variants per platform (X/Twitter, LinkedIn, Instagram
  caption, Threads, a short newsletter blurb), each respecting length/voice norms.
- Hashtag sets, A/B headline variants, a week's worth of teaser posts from one launch.

```bash
python3 "$SKILL/scripts/gemini.py" --model pro --system "Social media manager for <brand>" \
  "Turn this launch note into posts for X (<=280), LinkedIn (professional), and an Instagram caption with hashtags. Source: <paste announcement>. Match brand voice: <voice>."
```

**Keep on Claude / a script with your tokens:**
- **The actual posting is NEVER Gemini's** — it has no access to your accounts. Publishing goes
  through a script holding *your* platform API tokens, or you post manually. (When the apps are
  live and you want this automated, that's a separate posting-script project — this skill only
  drafts the content.)
- Final approval of voice, claims, and timing; nothing auto-publishes unreviewed.

**Verify:** read each variant for brand voice and accurate claims, check lengths against
platform limits, and confirm *you* approve before anything is posted.

---

## The seam, restated

For every one of these: **offload the words, keep the action.** If a step would deploy, commit,
post, charge, or run against a live service or your credentials, it stays on Claude or a script
you control — because Gemini structurally can't do it, and pretending it can is how a workflow
goes wrong.
