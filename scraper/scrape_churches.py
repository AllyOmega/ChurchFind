#!/usr/bin/env python3
"""Scrape US church data from OpenStreetMap via the Overpass API.

Queries run one state at a time. Splitting by state keeps each request inside
Overpass's timeout and gives the site data files small enough to lazy-load, so
a visitor searching in Ohio never downloads Texas.

Usage:
    python scraper/scrape_churches.py                 # all states
    python scraper/scrape_churches.py --states CO UT  # just a couple
    python scraper/scrape_churches.py --force         # re-fetch cached states
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from normalize import FAMILY_LABELS, normalize
from states import STATE_CENTERS, STATE_NAMES, STATES

# Mirrors are tried in order. If the primary is busy (Overpass returns 429 or
# 504 when its slots are full) the next one usually answers.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

USER_AGENT = "ChurchFind/1.0 (+https://github.com/AllyOmega/ChurchFind)"

QUERY_TIMEOUT = 300
MAX_ATTEMPTS = 4
PAUSE_BETWEEN_STATES = 4  # be a polite client of a free, shared API

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "states"

# Column order for the compact row format written to disk.
FIELDS = [
    "id", "name", "denomination", "family", "address", "city", "state",
    "postcode", "lat", "lon", "website", "phone", "email", "services",
    "hours", "wheelchair",
]


def build_query(state_code):
    """Overpass QL for every Christian place of worship in one state.

    `nwr` covers nodes, ways and relations -- a church may be mapped as a point,
    a building outline, or a multipolygon. `out center` collapses the latter two
    to a single coordinate.
    """
    return f"""
[out:json][timeout:{QUERY_TIMEOUT}];
area["ISO3166-2"="US-{state_code}"][admin_level=4]->.state;
(
  nwr["amenity"="place_of_worship"]["religion"="christian"](area.state);
);
out center tags;
""".strip()


def fetch(state_code):
    """Run the query for one state, rotating mirrors and backing off on failure."""
    query = build_query(state_code)
    payload = urllib.parse.urlencode({"data": query}).encode()

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        endpoint = ENDPOINTS[(attempt - 1) % len(ENDPOINTS)]
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT + 30) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            backoff = 5 * (2 ** (attempt - 1))
            print(f"    attempt {attempt} failed ({exc}); retrying in {backoff}s", flush=True)
            time.sleep(backoff)

    raise RuntimeError(f"all {MAX_ATTEMPTS} attempts failed for {state_code}: {last_error}")


def scrape_state(state_code):
    """Fetch, normalize and de-duplicate one state's churches."""
    raw = fetch(state_code)
    elements = raw.get("elements", [])

    records = {}
    for element in elements:
        record = normalize(element, state_code)
        if record is None:
            continue
        # A church mapped as both a node and a building outline shows up twice.
        # Key on name + rounded position to keep one copy of each.
        key = (record["name"].lower(), round(record["lat"], 3), round(record["lon"], 3))
        existing = records.get(key)
        if existing is None or _richness(record) > _richness(existing):
            records[key] = record

    churches = sorted(records.values(), key=lambda r: (r["city"], r["name"]))
    stamp = raw.get("osm3s", {}).get("timestamp_osm_base", "")
    return churches, len(elements), stamp


def _richness(record):
    """How many optional details a record carries -- used to pick between duplicates."""
    return sum(
        1 for field in ("address", "city", "postcode", "website", "phone", "denomination", "services")
        if record.get(field)
    )


def to_compact(churches):
    """Rows instead of objects: same data, roughly 60% fewer bytes over the wire."""
    return [[church[field] for field in FIELDS] for church in churches]


def write_state(state_code, churches, osm_timestamp):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{state_code}.json"
    payload = {
        "state": state_code,
        "name": STATE_NAMES[state_code],
        "count": len(churches),
        "osm_timestamp": osm_timestamp,
        "fields": FIELDS,
        "churches": to_compact(churches),
    }
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return path


def family_counts(churches):
    counts = {}
    for church in churches:
        counts[church["family"]] = counts.get(church["family"], 0) + 1
    return counts


def denomination_counts(churches):
    """Specific denominations within each family, for the second filter level."""
    counts = {}
    for church in churches:
        label = church.get("denomination", "")
        if label:
            counts.setdefault(church["family"], {})
            counts[church["family"]][label] = counts[church["family"]].get(label, 0) + 1
    return counts


def load_existing_summary(state_code):
    """Read a previously written state file so --force-less reruns can resume."""
    path = STATE_DIR / f"{state_code}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    index = {field: i for i, field in enumerate(payload["fields"])}
    churches = [
        {"family": row[index["family"]], "website": row[index["website"]],
         "phone": row[index["phone"]], "address": row[index["address"]],
         "denomination": row[index["denomination"]]}
        for row in payload["churches"]
    ]
    return payload, churches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", nargs="*", help="state codes to fetch (default: all)")
    parser.add_argument("--force", action="store_true", help="re-fetch states already on disk")
    args = parser.parse_args()

    wanted = [code for code, _ in STATES]
    if args.states:
        requested = [code.upper() for code in args.states]
        unknown = [code for code in requested if code not in STATE_NAMES]
        if unknown:
            parser.error(f"unknown state code(s): {', '.join(unknown)}")
        wanted = requested

    summaries = []
    totals = {"churches": 0, "with_website": 0, "with_phone": 0, "with_address": 0}
    families = {}
    denominations = {}
    failures = []
    osm_timestamps = []

    for position, state_code in enumerate(wanted, start=1):
        prefix = f"[{position}/{len(wanted)}] {state_code} {STATE_NAMES[state_code]}"

        cached = None if args.force else load_existing_summary(state_code)
        if cached:
            payload, churches = cached
            print(f"{prefix}: {payload['count']:,} churches (cached)", flush=True)
            osm_timestamp = payload.get("osm_timestamp", "")
        else:
            print(f"{prefix}: fetching...", flush=True)
            started = time.time()
            try:
                churches, raw_count, osm_timestamp = scrape_state(state_code)
            except RuntimeError as exc:
                print(f"    FAILED: {exc}", flush=True)
                failures.append(state_code)
                continue
            write_state(state_code, churches, osm_timestamp)
            elapsed = time.time() - started
            dropped = raw_count - len(churches)
            print(
                f"    {len(churches):,} churches kept, {dropped:,} unnamed/duplicate "
                f"dropped, {elapsed:.1f}s",
                flush=True,
            )
            time.sleep(PAUSE_BETWEEN_STATES)

        if osm_timestamp:
            osm_timestamps.append(osm_timestamp)

        counts = family_counts(churches)
        for family, count in counts.items():
            families[family] = families.get(family, 0) + count

        for family, labels in denomination_counts(churches).items():
            bucket = denominations.setdefault(family, {})
            for label, count in labels.items():
                bucket[label] = bucket.get(label, 0) + count

        with_website = sum(1 for c in churches if c["website"])
        with_phone = sum(1 for c in churches if c["phone"])
        with_address = sum(1 for c in churches if c["address"])
        totals["churches"] += len(churches)
        totals["with_website"] += with_website
        totals["with_phone"] += with_phone
        totals["with_address"] += with_address

        center = STATE_CENTERS.get(state_code, (39.5, -98.35))
        summaries.append({
            "code": state_code,
            "name": STATE_NAMES[state_code],
            "count": len(churches),
            "lat": center[0],
            "lon": center[1],
        })

    if failures:
        print(f"\nStates that could not be fetched: {', '.join(failures)}", flush=True)

    summaries.sort(key=lambda s: s["name"])
    index = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "osm_snapshot": max(osm_timestamps) if osm_timestamps else "",
        "source": "OpenStreetMap via Overpass API",
        "license": "ODbL 1.0",
        "totals": totals,
        # Biggest family first, except "unknown" -- it is the largest bucket in
        # OSM and leading the filter list with it would bury the real choices.
        "families": {
            family: {"label": FAMILY_LABELS.get(family, family), "count": count}
            for family, count in sorted(
                families.items(), key=lambda kv: (kv[0] in ("unknown", "other"), -kv[1])
            )
        },
        # Specific denominations nested under their family, so the site can offer
        # "Baptist" and then "Southern Baptist" without loading a state file.
        "denominations": {
            family: [
                {"label": label, "count": count}
                for label, count in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            for family, labels in sorted(denominations.items())
        },
        "states": summaries,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))

    print(f"\nTotal: {totals['churches']:,} churches across {len(summaries)} states")
    print(f"  with street address: {totals['with_address']:,}")
    print(f"  with website:        {totals['with_website']:,}")
    print(f"  with phone:          {totals['with_phone']:,}")
    print(f"Wrote {DATA_DIR / 'index.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
