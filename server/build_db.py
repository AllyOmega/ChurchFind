#!/usr/bin/env python3
"""Load data/states/*.json into SQLite.

Rebuilds the church tables from scratch every time -- they are a cache of the
scrape, not a system of record. Account tables are left alone, so re-importing a
fresh snapshot does not touch anyone's login or saved churches.

Usage:
    python server/build_db.py                # rebuild church tables
    python server/build_db.py --db /tmp/x.db # somewhere else
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import churchmanship  # noqa: E402
import service_times  # noqa: E402
from db import connect, init_schema  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "states"
INDEX_PATH = ROOT / "data" / "index.json"
CHURCHMANSHIP_PATH = ROOT / "data" / "churchmanship.json"

COLUMNS = [
    "id", "name", "denomination", "family", "address", "city", "state",
    "postcode", "lat", "lon", "website", "phone", "email", "services",
    "hours", "wheelchair", "hearing_loop", "toilets_wheelchair", "updated",
]

# Added after the first scrape. A data/ tree written by an older scraper simply
# lacks them, and that should degrade to empty rather than refusing to build --
# re-scraping 51 states takes the better part of an hour.
OPTIONAL = {"hearing_loop", "toilets_wheelchair", "updated"}

# Derived at build time rather than stored by the scraper: the parser lives with
# the site, so improving it only needs a rebuild, not a 45-minute re-scrape.
DERIVED = ["service_pairs", "service_text"]


def load_state(path):
    """Read one state file and yield rows in COLUMNS order.

    The file stores its own column order in `fields`, so a schema change in the
    scraper reorders itself here instead of silently shifting every value.
    """
    payload = json.loads(path.read_text())
    position = {field: i for i, field in enumerate(payload["fields"])}
    missing = [c for c in COLUMNS if c not in position and c not in OPTIONAL]
    if missing:
        raise SystemExit(f"{path.name} is missing column(s): {', '.join(missing)}")

    for row in payload["churches"]:
        values = [row[position[column]] if column in position else ""
                  for column in COLUMNS]
        raw_services = values[COLUMNS.index("services")]
        yield tuple(values) + (
            service_times.to_pairs(raw_services),
            service_times.describe(raw_services),
        )


def rebuild(connection):
    cursor = connection.cursor()

    # Order matters: saved_churches and correction_reports point at churches, so
    # the delete has to be a replace-in-place rather than a drop.
    #
    # churches_fts is external-content FTS5 -- a plain DELETE makes it read the
    # content table to work out which terms to remove, and reports "database disk
    # image is malformed" the moment the two disagree. 'delete-all' is the
    # documented way to empty one.
    cursor.execute("INSERT INTO churches_fts(churches_fts) VALUES('delete-all')")
    cursor.execute("DELETE FROM churches_geo")
    cursor.execute("DELETE FROM churches")

    all_columns = COLUMNS + DERIVED
    placeholders = ",".join("?" * len(all_columns))
    # OR IGNORE, not a plain INSERT: a church sitting on a state line comes back
    # from both states' Overpass queries, so the same OSM id appears in two
    # files. First file wins -- the rows are identical anyway.
    insert = f"INSERT OR IGNORE INTO churches ({','.join(all_columns)}) VALUES ({placeholders})"

    total = 0
    seen = set()
    duplicates = []
    for path in sorted(STATE_DIR.glob("*.json")):
        rows = list(load_state(path))
        for row in rows:
            if row[0] in seen:
                duplicates.append((row[0], row[1], path.stem))
            seen.add(row[0])
        cursor.executemany(insert, rows)
        total += len(rows)
        print(f"  {path.stem}: {len(rows):,}", flush=True)

    if duplicates:
        print(f"\n  {len(duplicates)} border church(es) skipped as duplicates:")
        for church_id, name, state in duplicates[:10]:
            print(f"    {church_id} {name} (also in {state})")
    total -= len(duplicates)

    # Degenerate boxes -- a point is a box with min == max.
    cursor.execute("""
        INSERT INTO churches_geo (id, min_lat, max_lat, min_lon, max_lon)
        SELECT rowid, lat, lat, lon, lon FROM churches
    """)

    # 'rebuild' repopulates the index straight from the content table, so the
    # two cannot drift apart the way a hand-written INSERT ... SELECT can.
    cursor.execute("INSERT INTO churches_fts(churches_fts) VALUES('rebuild')")

    if INDEX_PATH.exists():
        index = json.loads(INDEX_PATH.read_text())
        cursor.executemany(
            "INSERT OR REPLACE INTO dataset_meta (key, value) VALUES (?, ?)",
            [
                ("osm_snapshot", index.get("osm_snapshot", "")),
                ("generated_at", index.get("generated_at", "")),
                ("source", index.get("source", "")),
                ("license", index.get("license", "")),
                ("church_count", str(total)),
            ],
        )

    load_churchmanship(connection, cursor)

    connection.commit()
    cursor.execute("ANALYZE")
    connection.commit()
    return total


def load_churchmanship(connection, cursor):
    """Apply scraped churchmanship scores, preserving any user votes.

    The scraped half is replaced wholesale; the blended columns are then
    recomputed from it plus whatever votes exist. A re-scrape therefore never
    discards a submission.
    """
    if not CHURCHMANSHIP_PATH.exists():
        return 0
    scores = json.loads(CHURCHMANSHIP_PATH.read_text()).get("scores", {})
    if not scores:
        return 0

    known = {row[0] for row in cursor.execute("SELECT id FROM churches")}
    applied = 0
    for church_id, score in scores.items():
        if church_id not in known:
            continue
        cursor.execute(
            """INSERT INTO churchmanship
                 (church_id, scraped_ceremonial, scraped_theology, scraped_confidence, evidence)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(church_id) DO UPDATE SET
                 scraped_ceremonial = excluded.scraped_ceremonial,
                 scraped_theology   = excluded.scraped_theology,
                 scraped_confidence = excluded.scraped_confidence,
                 evidence           = excluded.evidence""",
            (church_id, score["ceremonial"], score["theology"], score["confidence"],
             ",".join(score.get("matched", []))),
        )
        applied += 1

    connection.commit()
    for church_id in scores:
        if church_id in known:
            recompute(connection, church_id)
    print(f"\n  Applied {applied:,} scraped churchmanship scores.")
    return applied


def recompute(connection, church_id):
    """Blend the scraped estimate with user votes and store the result."""
    row = connection.execute(
        "SELECT scraped_ceremonial, scraped_theology, scraped_confidence "
        "FROM churchmanship WHERE church_id = ?", (church_id,)
    ).fetchone()
    scraped = None
    if row and row["scraped_confidence"]:
        scraped = {"ceremonial": row["scraped_ceremonial"],
                   "theology": row["scraped_theology"],
                   "confidence": row["scraped_confidence"]}

    votes = [(v["ceremonial"], v["theology"]) for v in connection.execute(
        "SELECT ceremonial, theology FROM churchmanship_votes WHERE church_id = ?",
        (church_id,))]

    blended = churchmanship.blend(scraped, votes)
    if blended is None:
        connection.execute("DELETE FROM churchmanship WHERE church_id = ? "
                           "AND scraped_confidence = 0", (church_id,))
        connection.commit()
        return None

    connection.execute(
        """INSERT INTO churchmanship (church_id, ceremonial, theology, confidence,
                                      votes, source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(church_id) DO UPDATE SET
             ceremonial = excluded.ceremonial, theology = excluded.theology,
             confidence = excluded.confidence, votes = excluded.votes,
             source = excluded.source, updated_at = excluded.updated_at""",
        (church_id, blended["ceremonial"], blended["theology"], blended["confidence"],
         blended["votes"], blended["source"]),
    )
    connection.commit()
    return blended


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="database file (default: server/churchfind.db)")
    args = parser.parse_args()

    if not STATE_DIR.exists():
        raise SystemExit(f"{STATE_DIR} does not exist. Run the scraper first.")

    connection = connect(args.db)
    init_schema(connection)

    print("Loading state files...")
    total = rebuild(connection)

    users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"\nLoaded {total:,} churches. {users} account(s) left untouched.")
    print(f"Database: {connection.execute('PRAGMA database_list').fetchone()[2]}")
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
