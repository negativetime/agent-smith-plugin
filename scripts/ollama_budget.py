#!/usr/bin/env python3
"""Track the Ollama Cloud monthly allowance and say whether we are on pace.

    python3 ollama_budget.py              # poll, log, report
    python3 ollama_budget.py --no-log     # report without appending an observation
    python3 ollama_budget.py --allowance 60 --resets 2026-10-05

Ollama's `GET /api/usage` reports `limits.monthly.usage` as a FRACTION of the
included allowance, and `activity.cost` as out-of-pocket overage beyond it. On the
$20/mo plan the allowance is $60 of usage, so `cost` reads 0.00000 while `usage`
climbs — spending the budget means driving that fraction toward 1.0, and dollars
never appear until you have overspent.

There is no billing endpoint (subscription/billing/account/plan/limits all 404 as
of 2026-09-05), so the cycle reset date cannot be read from the API. This script
therefore LOGS each observation and infers the reset empirically: when the usage
fraction drops, a new cycle began. Pass --resets to pin it explicitly.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

USAGE_URL = "https://ollama.com/api/usage"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                   "ollama_usage.jsonl")


def fetch(key):
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def observations():
    if not os.path.isfile(LOG):
        return []
    out = []
    with open(LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass          # a truncated tail must not break reporting
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allowance", type=float, default=60.0,
                    help="included monthly usage allowance in dollars (default 60)")
    ap.add_argument("--resets", help="cycle reset date YYYY-MM-DD, if known")
    ap.add_argument("--no-log", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("OLLAMA_API_KEY", "")
    if not key:
        sys.exit("OLLAMA_API_KEY is not set (it lives in ~/.zshrc; a session started "
                 "before it was added will not see it)")

    d = fetch(key)
    monthly = (d.get("limits") or {}).get("monthly") or {}
    frac = float(monthly.get("usage") or 0.0)
    models = monthly.get("models") or []
    reqs = sum(m.get("request_count", 0) for m in models)
    overage = float((d.get("activity") or {}).get("cost") or 0.0)
    now = datetime.now(timezone.utc)

    spent = frac * a.allowance
    left = a.allowance - spent

    print(f"Ollama Cloud — {now.astimezone():%Y-%m-%d %H:%M}")
    print(f"  used     {frac*100:5.1f}%  = ${spent:6.2f} of ${a.allowance:.2f}")
    print(f"  left            ${left:6.2f}")
    print(f"  requests  {reqs:,} this cycle across {len(models)} models")
    if overage:
        print(f"  ⚠ OVERAGE ${overage:.5f} — you are past the included allowance")

    prior = observations()
    # Empirical reset detection: the fraction only ever climbs within a cycle.
    cycle = [o for o in prior]
    for i in range(len(prior) - 1, 0, -1):
        if prior[i]["usage"] < prior[i - 1]["usage"] - 1e-9:
            cycle = prior[i:]
            print(f"  cycle reset detected at {prior[i]['at'][:10]}")
            break

    if cycle:
        first = cycle[0]
        t0 = datetime.fromisoformat(first["at"])
        days = max((now - t0).total_seconds() / 86400, 1e-6)
        burned = frac - first["usage"]
        if days >= 0.5 and burned > 0:
            rate = burned / days
            print(f"  burn     {rate*100:5.2f}%/day  (${rate*a.allowance:.2f}/day) "
                  f"over {days:.1f}d")
            if rate > 0:
                print(f"  at this rate the allowance lasts "
                      f"{(1.0-frac)/rate:.1f} more days")

    if a.resets:
        end = datetime.fromisoformat(a.resets).replace(tzinfo=timezone.utc)
        days_left = max((end - now).total_seconds() / 86400, 0.0)
        if days_left:
            need = (1.0 - frac) / days_left
            print(f"  to finish by {a.resets}: spend {need*100:.2f}%/day "
                  f"(${need*a.allowance:.2f}/day) over {days_left:.1f}d")

    if models:
        print("  by model:")
        for m in sorted(models, key=lambda x: -x.get("request_count", 0))[:8]:
            print(f"    {m.get('name',''):28} {m.get('request_count',0):>5}")

    if not a.no_log:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"at": now.isoformat(), "usage": frac,
                                "requests": reqs, "overage": overage}) + "\n")


if __name__ == "__main__":
    main()
