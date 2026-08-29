#!/usr/bin/env python3
"""research-shape eval: z.ai GLM-5.2 + web_search  vs  gemini-pro + Google grounding.

Every ground-truth value below was read from the project's OWN primary source by
Claude on 2026-08-10 (swift.org, python.org, nodejs.org, go.dev, postgresql.org,
blog.rust-lang.org, kubernetes.io, djangoproject.com, ruby-lang.org, ziglang.org)
— NOT from any model, or the eval would be grading a model against a model.

Scope honesty: this measures the "current release fact + cite it" slice of the
research shape. It is the slice that failed on 2026-08-10 (GLM claimed Swift 6.4
while its own top ref said 6.3), and it is objectively gradable. It does NOT
measure open-ended synthesis research.
"""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys

GEMINI = os.path.expanduser("~/.claude/skills/agent-smith/scripts/gemini.py")

# accept: any of these strings appearing = the right version was named.
# reject: naming one of these = a wrong/hallucinated version (the failure mode).
QUESTIONS = [
    dict(id="swift",    q="What is the latest released version of Swift?",
         accept=[r"6\.3"], reject=[r"6\.4", r"6\.5", r"7\.0"], truth="6.3 (6.3.3), 2026-03-24"),
    dict(id="python",   q="What is the latest stable release of Python?",
         accept=[r"3\.14\.7"], reject=[r"3\.13\b", r"3\.15"], truth="3.14.7, 2026-08-05"),
    dict(id="node",     q="What is the current Node.js LTS version?",
         accept=[r"24\.19\.0", r"\b24\.19\b"], reject=[r"22\.\d", r"20\.\d"], truth="v24.19.0"),
    dict(id="go",       q="What is the latest released version of Go?",
         accept=[r"1\.26"], reject=[r"1\.25", r"1\.27"], truth="1.26.5 patch / 1.26.0 major"),
    dict(id="postgres", q="What is the latest stable release of PostgreSQL?",
         accept=[r"18\.4"], reject=[r"17\.\d", r"18\.[0-3]\b"], truth="18.4, 2026-05-14"),
    dict(id="rust",     q="What is the latest released version of Rust?",
         accept=[r"1\.97\.1", r"1\.97\b"], reject=[r"1\.96", r"1\.98"], truth="1.97.1, 2026-07-16"),
    dict(id="k8s",      q="What is the latest released version of Kubernetes?",
         accept=[r"1\.36\.2", r"1\.36\b"], reject=[r"1\.35", r"1\.34", r"1\.37"], truth="1.36.2, 2026-06-09"),
    dict(id="django",   q="What is the latest official release of Django?",
         accept=[r"\b6\.1\b"], reject=[r"\b5\.\d", r"\b6\.2\b"], truth="6.1"),
    dict(id="ruby",     q="What is the current stable release of Ruby?",
         accept=[r"4\.0\.6", r"\b4\.0\b"], reject=[r"\b3\.\d\.\d"], truth="4.0.6"),
    dict(id="zig",      q="What is the latest tagged release of Zig, and is there a 1.0 stable release yet?",
         accept=[r"0\.16"], reject=[r"\b1\.0\b.*(stable|released)", r"0\.15\b"], truth="0.16.0, 2026-04-13; no 1.0"),
]

PROMPT_SUFFIX = (" Answer in at most two sentences. State the version number "
                 "explicitly and cite your sources.")

LANES = {
    "glm+zai": [sys.executable, GEMINI, "--backend", "openai", "--base-url",
                "zai-coding", "--model", "glm-5.2", "--search"],
    "gemini-pro": [sys.executable, GEMINI, "--model", "pro", "--search"],
}

SRC_RE = re.compile(r"^\s+(?:- \[[^\]]*\]\s*|- )(.*)$")
VER_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")


def run(lane, cmd, item):
    full = cmd + [item["q"] + PROMPT_SUFFIX, "--tag", "gym-eval",
                  "--purpose", f"research-eval:{item['id']}:{lane}"]
    try:
        p = subprocess.run(full, capture_output=True, text=True, timeout=420)
        answer, err = p.stdout.strip(), p.stderr
    except subprocess.TimeoutExpired:
        answer, err = "", "TIMEOUT"

    # sources block lives in stderr for both lanes
    srcs, grab = [], False
    for line in err.splitlines():
        if line.startswith("sources:"):
            grab = True
            continue
        if grab:
            m = SRC_RE.match(line)
            if m:
                srcs.append(m.group(1).strip())
            elif line.strip() and not line.startswith(" "):
                grab = False

    hit = any(re.search(a, answer, re.I) for a in item["accept"])
    bad = [r for r in item["reject"] if re.search(r, answer, re.I)]
    empty = not answer

    # self-consistency proxy: did the answer name a version that appears NOWHERE
    # in its own returned source titles, while some other version does? That is
    # the exact 2026-08-10 Swift-6.4 failure.
    ans_vers = set(VER_RE.findall(answer))
    src_vers = set(VER_RE.findall(" ".join(srcs)))
    contradicts = bool(ans_vers and src_vers and not (ans_vers & src_vers))

    # A `reject` hit is a REVIEW FLAG, never an automatic fail. Measured 2026-08-10:
    # auto-failing on them mis-scored 4 of 20 answers. Verbose lanes legitimately
    # mention adjacent versions ("while Go 1.27 is in RC") and negate them ("there
    # is no 1.0 stable release") — the regex sees a mention, not a claim. So an
    # answer that hits BOTH accept and reject is REVIEW: grade it by eye.
    needs_eye = bool(hit and bad)
    return dict(id=item["id"], lane=lane, ok=(hit and not empty),
                review=needs_eye, hit=hit, wrong=bad, empty=empty,
                contradicts=contradicts, answer=answer,
                n_src=len(srcs), srcs=srcs[:3])


jobs = [(lane, cmd, item) for item in QUESTIONS for lane, cmd in LANES.items()]
results = []
with cf.ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(run, l, c, i): (l, i["id"]) for l, c, i in jobs}
    for f in cf.as_completed(futs):
        r = f.result()
        results.append(r)
        print(f"  [{r['lane']:11s}] {r['id']:9s} "
              f"{'REVIEW' if r['review'] else ('PASS' if r['ok'] else 'FAIL')}"
              f"  srcs={r['n_src']}"
              f"{'  EMPTY' if r['empty'] else ''}"
              f"{'  WRONG:' + ','.join(r['wrong']) if r['wrong'] else ''}"
              f"{'  SELF-CONTRADICTS' if r['contradicts'] else ''}", flush=True)

print("\n" + "=" * 78)
for lane in LANES:
    rs = [r for r in results if r["lane"] == lane]
    print(f"{lane:12s}  {sum(r['ok'] for r in rs)}/{len(rs)} correct   "
          f"empty={sum(r['empty'] for r in rs)}  "
          f"self-contradicting={sum(r['contradicts'] for r in rs)}  "
          f"avg sources={sum(r['n_src'] for r in rs)/max(len(rs),1):.1f}")

print("\n--- per-question ---")
for item in QUESTIONS:
    print(f"\n{item['id']}  (truth: {item['truth']})")
    for lane in LANES:
        r = next((x for x in results if x["id"] == item["id"] and x["lane"] == lane), None)
        if r:
            print(f"  {lane:11s} {'PASS' if r['ok'] else 'FAIL'}: "
                  f"{(r['answer'] or '(EMPTY)')[:260]}")

with open("research_eval_results.json", "w") as fh:
    json.dump(results, fh, indent=1)
print("\nraw -> research_eval_results.json")
