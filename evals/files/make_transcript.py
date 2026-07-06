#!/usr/bin/env python3
"""Generate a long, noisy QBR transcript with exactly 6 planted action items."""
import os

ACTIONS = [
    ("Maria Chen", "finalize the Helios pricing model", "2026-07-10"),
    ("Raj Patel", "ship the onboarding redesign", "2026-06-30"),
    ("Dana Whitfield", "renegotiate the AWS contract", "2026-08-15"),
    ("Tom Okafor", "hire two backend engineers", "2026-07-01"),
    ("Lena Sorensen", "publish the Q2 security audit", "2026-06-22"),
    ("Victor Alvarez", "launch the EU data-residency option", "2026-09-05"),
]

FILLER = [
    "So, picking up from where we left off last quarter, the numbers are broadly in line with plan.",
    "I think the bigger question is whether the funnel conversion holds once we raise prices.",
    "Right, and remember marketing wanted another two weeks before we commit to the webinar push.",
    "Customer success flagged three enterprise accounts that are wobbling on renewal.",
    "Let's not rat-hole on that — we can take it offline with the account team.",
    "Engineering velocity dipped in May because of the on-call load, but it's recovering.",
    "The board deck is due Friday and finance still needs the updated ARR bridge.",
    "Honestly the support backlog is the thing keeping me up at night right now.",
    "We saw a nice bump in trial signups after the conference, about 18% week over week.",
    "Procurement is dragging on the new vendor, so that timeline is at risk.",
    "Can we circle back to the roadmap slide? I don't think product and sales are aligned.",
    "Agreed. And the EU prospects keep asking about data residency on every call.",
    "Churn was flat at 3% which is fine, but logo churn ticked up among the SMB cohort.",
    "I'd rather we slow down and get the pricing right than rush it for the quarter.",
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "qbr_transcript.txt")
    lines = []
    lines.append("QUARTERLY BUSINESS REVIEW — Q2 2026 — Full Transcript")
    lines.append("Attendees: Maria, Raj, Dana, Tom, Lena, Victor, and the exec staff.")
    lines.append("=" * 70)
    lines.append("")
    speakers = ["Maria", "Raj", "Dana", "Tom", "Lena", "Victor", "Priya", "Marcus"]
    # Weave a lot of filler chatter, dropping one action item roughly every ~12 lines.
    action_idx = 0
    for block in range(6):
        for i in range(12):
            sp = speakers[(block * 7 + i) % len(speakers)]
            fl = FILLER[(block * 5 + i) % len(FILLER)]
            lines.append(f"{sp}: {fl}")
        owner, task, due = ACTIONS[action_idx]
        lines.append(
            f"Priya: Okay, let's make that an action item. ACTION ITEM — {owner} will "
            f"{task}; due {due}. Everyone good with that? ... Good, noted."
        )
        lines.append("")
        action_idx += 1
    # Tail filler so the action items aren't all clustered at the end.
    for i in range(20):
        sp = speakers[i % len(speakers)]
        lines.append(f"{sp}: {FILLER[i % len(FILLER)]}")
    lines.append("")
    lines.append("Priya: Great session everyone. Same time next quarter.")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {out} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
