#!/usr/bin/env python3
"""Rewrite the generated stats block in README.md from data/index.json.

Keeps the numbers in the README honest without anyone remembering to edit them:
the monthly refresh workflow runs this straight after a successful scrape.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "data" / "index.json"

BEGIN = "<!-- STATS:BEGIN -->"
END = "<!-- STATS:END -->"


def build_block(index):
    totals = index["totals"]
    total = totals["churches"]
    lines = [
        BEGIN,
        "",
        f"**{total:,} churches** across **{len(index['states'])} states and DC**, "
        f"from an OpenStreetMap snapshot taken **{(index.get('osm_snapshot') or '')[:10] or 'unknown'}**.",
        "",
        "| | Count | Share |",
        "|---|---:|---:|",
    ]

    for label, key in (
        ("Has a street address", "with_address"),
        ("Has a website", "with_website"),
        ("Has a phone number", "with_phone"),
    ):
        count = totals.get(key, 0)
        share = (count / total * 100) if total else 0
        lines.append(f"| {label} | {count:,} | {share:.0f}% |")

    lines += ["", "Denominational families, largest first:", "",
              "| Family | Churches | Share |", "|---|---:|---:|"]

    for info in index["families"].values():
        share = (info["count"] / total * 100) if total else 0
        lines.append(f"| {info['label']} | {info['count']:,} | {share:.1f}% |")

    biggest = sorted(index["states"], key=lambda s: -s["count"])[:10]
    lines += ["", "Ten largest state files:", "",
              "| State | Churches |", "|---|---:|"]
    for entry in biggest:
        lines.append(f"| {entry['name']} | {entry['count']:,} |")

    lines += ["", END]
    return "\n".join(lines)


def main():
    if not INDEX.exists():
        print(f"FAIL: {INDEX} is missing. Run scrape_churches.py first.")
        return 1
    if not README.exists():
        print(f"FAIL: {README} is missing.")
        return 1

    text = README.read_text()
    if BEGIN not in text or END not in text:
        print(f"FAIL: README.md has no {BEGIN} / {END} markers.")
        return 1

    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    README.write_text(head + build_block(json.loads(INDEX.read_text())) + tail)
    print("Updated the stats block in README.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
