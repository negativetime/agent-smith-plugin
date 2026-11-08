#!/usr/bin/env python3
"""Probes for `fleet_check.py --catalog`, the diff of what Gemini and z.ai OFFER against an
accepted baseline. Pure stdlib, no requests: SMITH_CATALOG_FIXTURE stands in for both model
lists, SMITH_CATALOG_IDS points at a temp baseline, SMITH_GYM_DIR at a fake agent-gym.

Why it exists (2026-11-07): new model ids reached this fleet only when someone happened to read
a changelog. The diff names them, and the gym command to measure one; it routes nothing.

    python3 -B scripts/test_fleet_catalog.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


root = tempfile.mkdtemp(prefix="catalog-")
gym = os.path.join(root, "gym")
os.makedirs(gym)
SIDE = os.path.join(root, "gym-was-executed")
# A MODELS table, plus code that would leave a file behind if anything RAN gym.py.
open(os.path.join(gym, "gym.py"), "w").write(f'''
import os
open({SIDE!r}, "w").write("ran")
MODELS = {{
    "gemini-pro": {{"backend": "gemini", "model": "pro", "agent_backend": "gemini"}},
    "gemini-3.8-flash": {{"backend": "gemini", "model": "gemini-3.8-flash"}},
    "zai-glm-5.3": {{"backend": "openai", "model": "glm-5.3", "api_key_env": os.environ.get("X", "ZAI")}},
}}
''')
SNAP = os.path.join(root, "catalog_ids.json")
FIX = os.path.join(root, "fixture.json")

BASE = {"gemini": {"gemini-pro-latest": "Gemini Pro Latest", "gemini-3.5-flash": "3.5-flash-05-2026"},
        "zai": {"glm-5.3": None, "glm-5.2": None}}


def run(fixture, *argv):
    json.dump(fixture, open(FIX, "w"))
    env = dict(os.environ, SMITH_CATALOG_FIXTURE=FIX, SMITH_CATALOG_IDS=SNAP, SMITH_GYM_DIR=gym)
    p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "fleet_check.py"), "--catalog", *argv],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout + p.stderr


rc, out = run(BASE)
check(rc == 2 and not os.path.exists(SNAP) and "gemini 2, zai 2" in out,
      "no baseline: exit 2, counts shown, nothing written")

rc, out = run(BASE, "--accept")
check(rc == 0 and json.load(open(SNAP)) == BASE, "--accept writes exactly the lists it read")

rc, out = run(BASE)
check(rc == 0 and "catalog unchanged: gemini 2, zai 2" in out, "same lists: exit 0, unchanged")

new = json.loads(json.dumps(BASE))
new["gemini"]["gemini-3.8-flash"] = "3.0"
new["zai"]["glm-5.4"] = None
rc, out = run(new)
check(rc == 1, "new ids: exit 1")
check("NEW     gemini: gemini-3.8-flash (version 3.0)" in out
      and f"cd {gym} && python3 -B gym.py --models gemini-3.8-flash" in out,
      "...a new id agent-gym already has a label for: the exact command to measure it")
check("NEW     zai: glm-5.4" in out and "no agent-gym label yet: copy the zai-glm-5.3 entry" in out,
      "...a new id with no label: says to add a MODELS entry first, and which one to copy")
check(not os.path.exists(SIDE), "...agent-gym's gym.py was parsed, never run")

alias = json.loads(json.dumps(BASE))
del alias["gemini"]["gemini-pro-latest"]
json.dump(alias, open(SNAP, "w"))
rc, out = run(BASE)
check("NEW     gemini: gemini-pro-latest" in out and "gym.py --models gemini-pro" in out,
      "a gym label written as shorthand (\"pro\") is found through gemini.py's ALIASES")
json.dump(BASE, open(SNAP, "w"))
check(json.load(open(SNAP)) == BASE, "...and the baseline is untouched until --accept")

gone = json.loads(json.dumps(BASE))
del gone["zai"]["glm-5.3"]
del gone["zai"]["glm-5.2"]
gone["zai"]["glm-6"] = None
rc, out = run(gone)
check(rc == 1 and "GONE    zai: glm-5.3  -> still named in" in out and "gemini.py" in out,
      "a gone id that agent-smith code still names (glm-5.3, a default route): says where")
check("GONE    zai: glm-5.2" in out, "...every gone id is listed")

ver = json.loads(json.dumps(BASE))
ver["gemini"]["gemini-3.5-flash"] = "3.5-flash-09-2026"
rc, out = run(ver)
check(rc == 1 and "CHANGED gemini: gemini-3.5-flash version 3.5-flash-05-2026 -> 3.5-flash-09-2026" in out,
      "same id, new version string: CHANGED")

half = {"gemini": BASE["gemini"], "zai": {"error": "HTTP Error 401: Unauthorized"}}
rc, out = run(half)
check(rc == 3 and "SKIPPED zai: could not read its model list (HTTP Error 401: Unauthorized)" in out
      and "GONE" not in out, "one provider unreadable: exit 3, SKIPPED, and its models are NOT reported gone")
rc, out = run(half, "--accept")
check(rc == 3 and json.load(open(SNAP)) == BASE and "Nothing written" in out,
      "--accept with a provider unreadable: exit 3, the baseline is kept as it was")

half_new = {"gemini": new["gemini"], "zai": {"error": "timeout"}}
rc, out = run(half_new)
check(rc == 1 and "NEW     gemini: gemini-3.8-flash" in out and "SKIPPED zai" in out,
      "a change on the readable provider still shows (exit 1) beside the skip")

empty = {"gemini": BASE["gemini"], "zai": {}}
rc, out = run(empty)
check(rc == 3 and "the provider returned no models" in out and "GONE" not in out,
      "an EMPTY list is a failed read, not 'every model gone'")

only = {"gemini": BASE["gemini"], "zai": BASE["zai"]}
json.dump({"gemini": BASE["gemini"]}, open(SNAP, "w"))
rc, out = run(only)
check(rc == 0 and "zai: not in the baseline yet (2 models)" in out and "NEW     zai" not in out,
      "a provider missing from the baseline: one line, not every model as NEW")

# ---- the local identity check: a stopped daemon is not an empty fleet --------------------------
bindir = os.path.join(root, "bin")
os.makedirs(bindir)
fake = os.path.join(bindir, "ollama")
idsnap = os.path.join(root, "fleet_ids.json")
json.dump({"gpt-oss:20b": "17052f91a42e", "llama3.2:3b": "a80c4f17acd5"}, open(idsnap, "w"))


def fleet(script):
    open(fake, "w").write("#!/bin/sh\n" + script)
    os.chmod(fake, 0o755)
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], SMITH_FLEET_IDS=idsnap)
    p = subprocess.run([sys.executable, "-B", os.path.join(_HERE, "fleet_check.py")], env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout + p.stderr


rc, out = fleet("echo \"Error: could not connect to ollama server, run 'ollama serve' to start it\"\nexit 0\n")
check(rc == 3 and "MISSING" not in out and "could not read the local fleet" in out,
      "Ollama not running (error, exit 0, no table): exit 3, NOT every model MISSING")
rc, out = fleet("echo boom >&2\nexit 1\n")
check(rc == 3 and "MISSING" not in out and "boom" in out, "ollama list exits 1: exit 3, its error shown")
rc, out = fleet("printf 'NAME ID SIZE MODIFIED\\ngpt-oss:20b 17052f91a42e 13GB x\\n'\nexit 1\n")
check(rc == 3 and "MISSING" not in out,
      "a table followed by a failing exit (a partial list?): exit 3, nothing reported MISSING")
rc, out = fleet("printf 'NAME ID SIZE MODIFIED\\ngpt-oss:20b 17052f91a42e 13GB x\\nllama3.2:3b a80c4f17acd5 2GB x\\n'\n")
check(rc == 0 and "fleet identity OK: 2 baselined models unchanged" in out, "a real table that matches: exit 0")
rc, out = fleet("printf 'NAME ID SIZE MODIFIED\\ngpt-oss:20b 99999999ffff 13GB x\\n'\n")
check(rc == 1 and "CHANGED: gpt-oss:20b" in out and "MISSING: llama3.2:3b" in out,
      "a real table with a new digest and a removed model: CHANGED and MISSING, exit 1")

shutil.rmtree(root, ignore_errors=True)
print("\nALL PASS" if all_pass else "\nSOME FAILED")
sys.exit(0 if all_pass else 1)
