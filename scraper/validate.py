#!/usr/bin/env python3
"""Sanity-check the generated data/ tree before it gets committed or deployed.

Catches the failure modes that actually happen: a state file truncated by an
interrupted scrape, an index whose totals drifted from the files it describes,
coordinates that landed outside the US after a bad parse.

Exits non-zero if anything fails, so it drops straight into CI.
"""

import json
import sys
from pathlib import Path

from states import STATE_NAMES

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "states"

# Generous bounds covering the lower 48, Alaska and Hawaii.
LAT_RANGE = (18.0, 72.0)
LON_RANGE = (-180.0, -66.0)

REQUIRED_FIELDS = ["id", "name", "denomination", "family", "lat", "lon"]


def fail(errors, message):
    errors.append(message)


def validate_state_file(path, errors):
    """Check one state file and return (code, church_count)."""
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        fail(errors, f"{path.name}: unreadable ({exc})")
        return None, 0

    code = payload.get("state")
    if code not in STATE_NAMES:
        fail(errors, f"{path.name}: unknown state code {code!r}")
        return None, 0

    fields = payload.get("fields") or []
    missing = [field for field in REQUIRED_FIELDS if field not in fields]
    if missing:
        fail(errors, f"{path.name}: missing field(s) {', '.join(missing)}")
        return code, 0

    index = {field: i for i, field in enumerate(fields)}
    rows = payload.get("churches") or []

    if payload.get("count") != len(rows):
        fail(errors, f"{path.name}: count says {payload.get('count')} but has {len(rows)} rows")

    seen_ids = set()
    bad_shape = bad_name = bad_coord = duplicate = 0

    for row in rows:
        if len(row) != len(fields):
            bad_shape += 1
            continue
        if not str(row[index["name"]]).strip():
            bad_name += 1
        identifier = row[index["id"]]
        if identifier in seen_ids:
            duplicate += 1
        seen_ids.add(identifier)
        lat, lon = row[index["lat"]], row[index["lon"]]
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and LAT_RANGE[0] <= lat <= LAT_RANGE[1]
                and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            bad_coord += 1

    for label, count in (("malformed rows", bad_shape), ("blank names", bad_name),
                         ("duplicate ids", duplicate), ("out-of-range coordinates", bad_coord)):
        if count:
            fail(errors, f"{path.name}: {count} {label}")

    if not rows:
        fail(errors, f"{path.name}: no churches at all")

    return code, len(rows)


def main():
    errors = []

    index_path = DATA_DIR / "index.json"
    if not index_path.exists():
        print(f"FAIL: {index_path} is missing. Run scrape_churches.py first.")
        return 1

    index = json.loads(index_path.read_text())
    state_files = sorted(STATE_DIR.glob("*.json"))
    if not state_files:
        print(f"FAIL: no state files in {STATE_DIR}.")
        return 1

    counts = {}
    for path in state_files:
        code, count = validate_state_file(path, errors)
        if code:
            counts[code] = count

    # Every state the index advertises must have a file behind it, with a
    # matching count -- otherwise the site offers a link that 404s.
    for entry in index.get("states", []):
        code = entry["code"]
        if code not in counts:
            fail(errors, f"index lists {code} but data/states/{code}.json is missing")
        elif counts[code] != entry["count"]:
            fail(errors, f"index says {code} has {entry['count']} churches, file has {counts[code]}")

    indexed = {entry["code"] for entry in index.get("states", [])}
    for code in sorted(set(counts) - indexed):
        fail(errors, f"data/states/{code}.json exists but is not listed in the index")

    total = sum(counts.values())
    if index.get("totals", {}).get("churches") != total:
        fail(errors, f"index total is {index.get('totals', {}).get('churches')}, files hold {total}")

    for error in errors:
        print(f"FAIL: {error}")

    if errors:
        print(f"\n{len(errors)} problem(s) found.")
        return 1

    print(f"OK: {len(counts)} state files, {total:,} churches, index consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
