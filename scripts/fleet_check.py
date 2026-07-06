#!/usr/bin/env python3
"""Fleet identity check: catch models that changed under you.

`ollama pull` updates weights IN PLACE under the same tag — a model that earned a
trusted tier can silently become a different model. The behavioral witness catches
drift eventually; this catches IDENTITY changes instantly.

    python3 fleet_check.py             # compare current digests vs the accepted snapshot
    python3 fleet_check.py --accept    # accept the current fleet as the new baseline

Exit codes: 0 = fleet matches baseline, 1 = changes detected, 2 = no baseline yet.
Snapshot lives at data/fleet_ids.json next to the ledger.
"""
import argparse
import json
import os
import subprocess
import sys

SNAP = os.environ.get("SMITH_FLEET_IDS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fleet_ids.json")


def current_fleet():
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                             timeout=30).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot run `ollama list`: {exc}", file=sys.stderr)
        sys.exit(2)
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
    args = ap.parse_args()

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
