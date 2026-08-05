#!/usr/bin/env python3
"""Re-apply normalisation rules to the state files already on disk.

The same argument as caching page text for the churchmanship lexicon: fetching
is the expensive, externally visible part, and the rules change more often than
the data does. A classification fix should not need a 45-minute re-scrape of 51
states against a public API that has spent this week returning 504s.

It matters for correctness, not just convenience. Rules applied only at scrape
time leave the state files stale, and the state files *are* the static build --
so a fix would land for the API and silently not for the deployed site, which is
the same parity trap that hid churchmanship and the service-time filter.

Only fields derived from other fields are touched. Nothing here invents data:
`denomination` and `family` are recomputed from the name and the raw OSM tag,
and everything else is copied through untouched.

Usage:
    python scraper/renormalize.py            # rewrite every state file
    python scraper/renormalize.py --dry-run  # report what would change
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "states"


def renormalize_state(path, dry_run=False):
    """Rewrite one state file. Returns the number of rows whose family changed."""
    payload = json.loads(path.read_text())
    fields = payload["fields"]
    try:
        name_at = fields.index("name")
        denom_at = fields.index("denomination")
        family_at = fields.index("family")
    except ValueError:
        return 0

    changed = 0
    for row in payload["churches"]:
        label, family = normalize._correct_from_name(
            row[name_at], row[denom_at], row[family_at])
        if family != row[family_at] or label != row[denom_at]:
            changed += 1
            row[denom_at], row[family_at] = label, family

    if changed and not dry_run:
        # Same temp-and-rename as the scraper: a megabyte of JSON is not one
        # write syscall, and a torn file takes a whole state offline.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        os.replace(temp, path)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    if not STATE_DIR.exists():
        raise SystemExit(f"{STATE_DIR} does not exist. Run the scraper first.")

    total = 0
    for path in sorted(STATE_DIR.glob("*.json")):
        changed = renormalize_state(path, dry_run=args.dry_run)
        if changed:
            print(f"  {path.stem}: {changed} row(s) reclassified")
            total += changed

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{total} row(s) {verb}.")
    if total and not args.dry_run:
        print("Run scraper/scrape_churches.py --states <none> is not needed; "
              "regenerate the index and rebuild:")
        print("  python -c \"import sys; sys.path.insert(0,'scraper'); "
              "import scrape_churches as s; s.write_index()\"")
        print("  python server/build_db.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
