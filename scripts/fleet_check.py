#!/usr/bin/env python3
"""Fleet identity check: catch models that changed under you.

`ollama pull` updates weights IN PLACE under the same tag — a model that earned a
trusted tier can silently become a different model. The behavioral witness catches
drift eventually; this catches IDENTITY changes instantly.

    python3 fleet_check.py             # compare current digests vs the accepted snapshot
    python3 fleet_check.py --accept    # accept the current fleet as the new baseline

Exit codes: 0 = fleet matches baseline, 1 = changes detected, 2 = no baseline yet, 3 = the fleet
could not be read (Ollama not running), so nothing was compared.
Snapshot lives at data/fleet_ids.json next to the ledger.

Catalog diff (2026-11-07): what the cloud providers OFFER, not what is pulled locally.

    python3 fleet_check.py --catalog            # Gemini + z.ai model lists vs data/catalog_ids.json
    python3 fleet_check.py --catalog --accept   # record the current lists as the baseline

It prints NEW ids (with the agent-gym command to measure one, or that it needs a MODELS entry
first), GONE ids (with the agent-smith code that still names one), and a Gemini model whose
version string changed. It tests nothing and routes nothing: a model earns a lane in the gym.
The `-latest` aliases report no target ("Gemini Pro Latest"), so a re-pointed alias is NOT seen
here; the behavioral witness and the ledger's grades are what catch that.
Exit codes: 0 no change, 1 changes, 2 no baseline yet, 3 a provider could not be read (its diff
is skipped, and --accept writes nothing, since a list missing one provider would make every one
of its models NEW on the next run). Keys: GEMINI_API_KEY, ZAI_API_KEY.
"""
import argparse
import ast
import json
import os
import subprocess
import sys
import urllib.request

SNAP = os.environ.get("SMITH_FLEET_IDS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fleet_ids.json")


_SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.environ.get("SMITH_CATALOG_IDS") or os.path.join(_SKILL, "data", "catalog_ids.json")
GYM_DIR = os.environ.get("SMITH_GYM_DIR") or os.path.expanduser("~/Developer/agent-gym")
GEMINI_MODELS = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000&key="
ZAI_MODELS = "https://api.z.ai/api/coding/paas/v4/models"   # the Coding Plan lane the routes use
PROVIDERS = ("gemini", "zai")


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_catalog():
    """{provider: {id: version or None}} and {provider: error} for the ones that failed. An empty
    list counts as a failure on every path. SMITH_CATALOG_FIXTURE (a JSON file of the same shape,
    an error as {"error": "..."}) stands in for the network, for the probes."""
    fixture = os.environ.get("SMITH_CATALOG_FIXTURE")
    cat, errs = (_read_fixture(fixture) if fixture else _read_network())
    for p, ids in list(cat.items()):
        if not ids:                     # an empty list is a failed read, not "every model gone"
            del cat[p]
            errs[p] = "the provider returned no models"
    return cat, errs


def _read_fixture(path):
    raw = json.load(open(path))
    cat = {p: raw[p] for p in PROVIDERS if isinstance(raw.get(p), dict) and "error" not in raw[p]}
    errs = {p: (raw.get(p) or {}).get("error", "missing from fixture") for p in PROVIDERS if p not in cat}
    return cat, errs


def _read_network():
    cat, errs = {}, {}
    try:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        d = _get_json(GEMINI_MODELS + key)
        if d.get("nextPageToken"):
            raise RuntimeError("the list came back in pages; this reads one page only")
        cat["gemini"] = {m["name"].replace("models/", ""): m.get("version")
                         for m in d.get("models", [])
                         if "generateContent" in m.get("supportedGenerationMethods", [])}
    except Exception as e:  # noqa: BLE001
        errs["gemini"] = str(e).replace(os.environ.get("GEMINI_API_KEY") or "\0", "<key>")
    try:
        key = os.environ.get("ZAI_API_KEY")
        if not key:
            raise RuntimeError("ZAI_API_KEY is not set")
        d = _get_json(ZAI_MODELS, {"Authorization": f"Bearer {key}"})
        cat["zai"] = {m["id"]: None for m in d.get("data", []) if m.get("id")}
    except Exception as e:  # noqa: BLE001
        errs["zai"] = str(e)
    return cat, errs


def _literal_dict(path, name):
    """A module-level `name = {...}` dict of constants, read with ast (nothing imported or run)."""
    try:
        tree = ast.parse(open(path).read())
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == name for t in node.targets)):
            return {k.value: v.value for k, v in zip(node.value.keys, node.value.values)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
    return {}


def gym_labels():
    """{(backend, model id): [labels]} from agent-gym's MODELS table, read with ast (nothing is
    imported or run). Empty when agent-gym is not there. A Gemini label's shorthand ("pro") is
    resolved through gemini.py's ALIASES, as gemini.py itself does ("gemini-pro-latest")."""
    aliases = _literal_dict(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini.py"),
                            "ALIASES")
    out = {}
    try:
        tree = ast.parse(open(os.path.join(GYM_DIR, "gym.py")).read())
    except (OSError, SyntaxError):
        return out
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == "MODELS" for t in node.targets)):
            for k, v in zip(node.value.keys, node.value.values):
                if not (isinstance(k, ast.Constant) and isinstance(v, ast.Dict)):
                    continue
                spec = {kk.value: vv.value for kk, vv in zip(v.keys, v.values)
                        if isinstance(kk, ast.Constant) and isinstance(vv, ast.Constant)}
                model = spec.get("model")
                if spec.get("backend") == "gemini":
                    model = aliases.get(model, model)
                out.setdefault((spec.get("backend"), model), []).append(k.value)
    return out


GYM_BACKEND = {"gemini": "gemini", "zai": "openai"}
GYM_TEMPLATE = {"gemini": "gemini-pro", "zai": "zai-glm-5.3"}


def code_mentions(model_id):
    """agent-smith scripts that name this id in quotes (a default route, a fallback)."""
    hits = []
    here = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(here)):
        if name.endswith(".py") and not name.startswith("test_"):
            try:
                text = open(os.path.join(here, name)).read()
            except OSError:
                continue
            if f'"{model_id}"' in text or f"'{model_id}'" in text:
                hits.append(name)
    return hits


def catalog(accept):
    cat, errs = fetch_catalog()
    for p, e in sorted(errs.items()):
        print(f"SKIPPED {p}: could not read its model list ({e})")
    if accept:
        if errs:
            print("Nothing written: a baseline missing a provider would make all its models NEW next time.")
            sys.exit(3)
        os.makedirs(os.path.dirname(CATALOG), exist_ok=True)
        tmp = CATALOG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cat, f, indent=1, sort_keys=True)
        os.replace(tmp, CATALOG)
        print("catalog baseline accepted: " + ", ".join(f"{p} {len(cat[p])}" for p in sorted(cat))
              + f" -> {CATALOG}")
        return
    if not os.path.isfile(CATALOG):
        print("No catalog baseline yet. Now: " + ", ".join(f"{p} {len(v)}" for p, v in sorted(cat.items()))
              + ". Run with --catalog --accept to record it.")
        sys.exit(2)
    base = json.load(open(CATALOG))
    labels = gym_labels()
    changes = 0
    for p in PROVIDERS:
        if p not in cat:
            continue
        old, new = base.get(p) or {}, cat[p]
        if p not in base:
            print(f"{p}: not in the baseline yet ({len(new)} models); --catalog --accept records it.")
            continue
        for m in sorted(set(new) - set(old)):
            changes += 1
            ver = f" (version {new[m]})" if new.get(m) and new[m] != m else ""
            have = labels.get((GYM_BACKEND[p], m))
            how = (f"measure it: cd {GYM_DIR} && python3 -B gym.py --models {','.join(have)}" if have
                   else f"no agent-gym label yet: copy the {GYM_TEMPLATE[p]} entry in {GYM_DIR}/gym.py "
                        f"MODELS, then gym.py --models <label>")
            print(f"NEW     {p}: {m}{ver}  -> {how}")
        for m in sorted(set(old) - set(new)):
            changes += 1
            used = code_mentions(m)
            print(f"GONE    {p}: {m}" + (f"  -> still named in {', '.join(used)}" if used else ""))
        for m in sorted(set(old) & set(new)):
            if old[m] != new[m] and old[m] is not None and new[m] is not None:
                changes += 1
                print(f"CHANGED {p}: {m} version {old[m]} -> {new[m]} (same id, a different build)")
    if changes:
        print(f"{changes} change(s). After reading them: --catalog --accept.")
        sys.exit(1)
    if errs:
        sys.exit(3)
    print("catalog unchanged: " + ", ".join(f"{p} {len(cat[p])}" for p in PROVIDERS if p in cat))


def current_fleet():
    # A daemon that is not running makes `ollama list` print its error and no table. That used
    # to read as an empty fleet, and every baselined model was reported MISSING (seen
    # 2026-11-07: 7 false MISSING lines with Ollama stopped). No NAME header = no reading.
    try:
        p = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot run `ollama list`: {exc}", file=sys.stderr)
        sys.exit(3)
    out = p.stdout
    if p.returncode != 0 or not out.lstrip().startswith("NAME"):
        why = (p.stderr or p.stdout).strip().splitlines()
        print(f"ERROR: could not read the local fleet (`ollama list`: "
              f"{why[0] if why else f'exit {p.returncode}'}). Nothing compared; start Ollama and re-run.",
              file=sys.stderr)
        sys.exit(3)
    fleet = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            fleet[parts[0]] = parts[1]  # NAME -> ID (digest prefix)
    return fleet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept", action="store_true",
                    help="accept the current fleet digests as the trusted baseline")
    ap.add_argument("--catalog", action="store_true",
                    help="diff the Gemini and z.ai model lists against data/catalog_ids.json")
    args = ap.parse_args()

    if args.catalog:
        catalog(args.accept)
        return
    fleet = current_fleet()
    if args.accept:
        os.makedirs(os.path.dirname(SNAP), exist_ok=True)
        with open(SNAP, "w") as f:
            json.dump(fleet, f, indent=1, sort_keys=True)
        print(f"baseline accepted: {len(fleet)} models -> {SNAP}")
        return

    if not os.path.isfile(SNAP):
        print(f"No baseline yet — run with --accept to record the current fleet.")
        sys.exit(2)
    base = json.load(open(SNAP))
    changed = {m: (base[m], fleet[m]) for m in base if m in fleet and base[m] != fleet[m]}
    missing = [m for m in base if m not in fleet]
    added = [m for m in fleet if m not in base]
    if not (changed or missing):
        print(f"fleet identity OK: {len(base)} baselined models unchanged"
              + (f" (+{len(added)} new, unbaselined)" if added else ""))
        return
    for m, (old, new) in changed.items():
        print(f"CHANGED: {m}  {old} -> {new}  — this is NOT the model that earned its "
              f"tier. Re-gate (gym run) or --accept deliberately.")
    for m in missing:
        print(f"MISSING: {m} (was {base[m]}) — removed or renamed.")
    sys.exit(1)


if __name__ == "__main__":
    main()
