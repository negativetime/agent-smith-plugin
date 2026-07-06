#!/usr/bin/env python3
"""Unit test for the code pre-flight helpers in gemini.py (pure stdlib, no model calls).

Runs preflight_python over a handful of mock strings and prints a classification
table, asserting each lands in its expected (parses?) bucket.
"""
import importlib.util
import os
import sys

# Import the helpers directly from gemini.py (it's a script, not a package).
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gemini", os.path.join(_HERE, "gemini.py"))
gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gemini)
preflight_python = gemini.preflight_python

# (label, mock_text, expected_ok)
CASES = [
    ("valid python",        "def add(a, b):\n    return a + b\n",                              True),
    ("syntax error",        "def broken(:\n    return 1\n",                                    False),
    ("prose, not code",     "Sure! Here is how you would approach the problem in plain words.", False),
    ("fenced valid code",   "```python\nimport math\nprint(math.pi)\n```",                     True),
    ("empty string",        "",                                                                True),
]

print(f"{'case':<20} {'expected':<9} {'got':<5} {'pass':<5} detail")
print("-" * 78)

all_pass = True
for label, text, expected in CASES:
    ok, err = preflight_python(text)
    passed = (ok == expected)
    all_pass = all_pass and passed
    detail = err if err else "(parses cleanly)"
    print(f"{label:<20} {str(expected):<9} {str(ok):<5} {('OK' if passed else 'FAIL'):<5} {detail}")

print("-" * 78)
print("ALL PASS" if all_pass else "SOME FAILED")
sys.exit(0 if all_pass else 1)
