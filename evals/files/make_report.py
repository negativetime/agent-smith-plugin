#!/usr/bin/env python3
"""Generate a large (~700KB) annual report woven around 3 strategic priorities,
with 3 distinctive 'headline metric' needles buried at scattered positions.
A correct executive summary must surface all three — there's no grep shortcut."""
import os

PRIORITIES = ["Project Helios", "the Atlas migration", "Northwind compliance"]

# The 3 needles — phrased naturally (no uniform tag), placed at ~25%, ~55%, ~85%.
NEEDLES = {
    0.25: "In a decision that will shape the next three years, the board ratified a "
          "47.3 million dollar capital allocation for Project Helios, the single largest "
          "infrastructure commitment in the company's history.",
    0.55: "Operationally, the Atlas migration crossed an important threshold this period: "
          "it is now 62 percent complete, with full production cutover targeted for the third "
          "quarter of 2027 and the legacy estate fully decommissioned thereafter.",
    0.85: "On the regulatory front, the results speak for themselves — Northwind compliance "
          "drove our external audit findings down from 214 the prior year to just 9, a result "
          "that materially de-risks our position with regulators.",
}

SENTENCES = [
    "The leadership team reaffirmed its commitment to {p} as a defining initiative for the year.",
    "Stakeholders across the organization continue to rally around {p} despite the inevitable headwinds.",
    "Progress on {p} was uneven across regions, but the underlying trajectory remains encouraging.",
    "Investment in {p} reflects a deliberate, patient view of where durable value is created.",
    "Cross-functional alignment on {p} improved markedly after the mid-year operating review.",
    "Risks associated with {p} were re-scored and folded into the enterprise risk register.",
    "Customer-facing teams reported that {p} is beginning to show up in retention conversations.",
    "Finance modeled several scenarios for {p}, stress-testing the assumptions behind the plan.",
    "The narrative around {p} matured from aspiration to disciplined, milestone-driven execution.",
    "Talent and hiring decisions were increasingly made in service of {p} rather than around it.",
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "annual_report.txt")
    target = 700_000
    lines = ["ANNUAL STRATEGIC REVIEW 2026 — FULL NARRATIVE", "=" * 70, ""]
    buf_len = sum(len(x) + 1 for x in lines)
    section = 0
    para = 0
    placed = set()
    while buf_len < target:
        section += 1
        lines.append(f"\nSECTION {section}. Strategic Context and Operating Notes")
        for _ in range(8):
            para += 1
            frac = buf_len / target
            # Drop a needle when we first cross each threshold.
            for thresh, text in NEEDLES.items():
                if thresh not in placed and frac >= thresh:
                    lines.append(text)
                    placed.add(thresh)
                    buf_len += len(text) + 1
            p = PRIORITIES[para % 3]
            p2 = PRIORITIES[(para + 1) % 3]
            s1 = SENTENCES[para % len(SENTENCES)].format(p=p)
            s2 = SENTENCES[(para + 3) % len(SENTENCES)].format(p=p2)
            s3 = SENTENCES[(para + 6) % len(SENTENCES)].format(p=PRIORITIES[(para + 2) % 3])
            line = f"{s1} {s2} {s3}"
            lines.append(line)
            buf_len += len(line) + 1
    # Ensure any unplaced needle still lands (in case the loop ended early).
    for thresh, text in NEEDLES.items():
        if thresh not in placed:
            lines.insert(len(lines) // 2, text)
    lines.append("\nEND OF REPORT.")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    size = os.path.getsize(out)
    print(f"wrote {out} ({size} bytes, {len(lines)} lines)")


if __name__ == "__main__":
    main()
