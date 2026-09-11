#!/usr/bin/env python3
"""Unit test for Ollama Cloud tag detection in gemini.py (pure stdlib, no model calls).

Two bugs found 2026-09-11 while probing `deepseek-v4.1-flash:cloud`:
  1. every ollama call printed "(local · free · unlimited)", `:cloud` tags included;
  2. cloud detection only matched `<name>:cloud`, but the registry ALSO spells cloud tags
     `<name>:<size>-cloud` (gpt-oss:120b-cloud, gpt-oss:20b-cloud). Those walked past the
     SoundCheck guard (_cloud_route) and the --batch cost guard as if they were local.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)

# (model tag, leaves this Mac?)
CASES = [
    ("deepseek-v4.1-flash:cloud", True),
    ("glm-5.3:cloud",             True),
    ("gpt-oss:120b-cloud",        True),   # registry spelling #2
    ("gpt-oss:20b-cloud",         True),   # one suffix away from the trusted local model
    ("gpt-oss:20b",               False),
    ("qwen3-vl:4b",               False),
    ("llama3.2:3b-obs",           False),  # a local custom tag with a dash suffix
    ("cloud",                     False),  # a bare NAME, no tag
    ("",                          False),
    (None,                        False),
]

all_pass = True


def check(ok, what):
    global all_pass
    all_pass &= ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}")


for model, cloud in CASES:
    label = gemini._ollama_route_label(model)
    check(("METERED" in label) == cloud and ("free" in label) != cloud,
          f"label        {model!r:<28} -> {label}")
    check(gemini._cloud_route("ollama", model) == cloud,
          f"_cloud_route {model!r:<28} -> {gemini._cloud_route('ollama', model)}  (SoundCheck guard)")
    if model:
        free, why = gemini._batch_cost_shape("ollama", model, None)
        check(free == (not cloud), f"batch cost   {model!r:<28} -> free={free}  ({why})")

# Every cloud-detection site must go through the helper, not a raw suffix test.
src = open(os.path.join(_HERE, "gemini.py"), encoding="utf-8").read()
check('.endswith(":cloud")' not in src, "no raw .endswith(\":cloud\") left in gemini.py")
check("(local · free · unlimited)\")" not in src and "_ollama_route_label(model)" in src,
      "meta line uses _ollama_route_label")

sys.exit(0 if all_pass else 1)
