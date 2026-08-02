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

    # DC is an entry in `states` but is not a state, so it gets counted separately.
    codes = {entry["code"] for entry in index["states"]}
    has_dc = "DC" in codes
    state_count = len(codes) - (1 if has_dc else 0)
    coverage = f"{state_count} states" + (" and DC" if has_dc else "")

    lines = [
        BEGIN,
        "",
        f"**{total:,} churches** across **{coverage}**, "
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

    # index.json orders these for the filter chips, which deliberately pushes
    # the huge "Unspecified" bucket to the end. A reference table wants plain
    # descending order instead.
    families = sorted(index["families"].values(), key=lambda info: -info["count"])
    for info in families:
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
