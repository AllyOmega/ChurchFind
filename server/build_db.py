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
import normalize  # noqa: E402
import service_times  # noqa: E402
from db import connect, init_schema  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "states"
INDEX_PATH = ROOT / "data" / "index.json"
CHURCHMANSHIP_PATH = ROOT / "data" / "churchmanship.json"
CHURCHMANSHIP_WIKI_PATH = ROOT / "data" / "churchmanship-wikipedia.json"
# What the no-server build reads: the two sources already merged, ready to use.
STATIC_CHURCHMANSHIP_PATH = ROOT / "data" / "churchmanship-merged.json"
# Service times read off parish websites, and the merged set the no-server build
# reads. OSM has a service time for 7% of Anglican churches; this is the rest.
WEB_TIMES_PATH = ROOT / "data" / "service-times-web.json"
STATIC_TIMES_PATH = ROOT / "data" / "service-times-merged.json"

COLUMNS = [
    "id", "name", "denomination", "family", "address", "city", "state",
    "postcode", "lat", "lon", "website", "phone", "email", "services",
    "hours", "wheelchair", "hearing_loop", "toilets_wheelchair", "updated",
    "wikipedia", "wikidata",
]

# Added after the first scrape. A data/ tree written by an older scraper simply
# lacks them, and that should degrade to empty rather than refusing to build --
# re-scraping 51 states takes the better part of an hour.
OPTIONAL = {"hearing_loop", "toilets_wheelchair", "updated", "wikipedia", "wikidata"}

# Derived at build time rather than stored by the scraper: the parser lives with
# the site, so improving it only needs a rebuild, not a 45-minute re-scrape.
DERIVED = ["service_pairs", "service_text", "service_source"]


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

    name_at = COLUMNS.index("name")
    denom_at = COLUMNS.index("denomination")
    family_at = COLUMNS.index("family")

    for row in payload["churches"]:
        values = [row[position[column]] if column in position else ""
                  for column in COLUMNS]

        # Applied here as well as in the scraper so a correction lands on the
        # next build rather than waiting on a 45-minute re-scrape of 51 states.
        # It is the scraper's rule, called rather than restated.
        values[denom_at], values[family_at] = normalize._correct_from_name(
            values[name_at], values[denom_at], values[family_at])
        raw_services = values[COLUMNS.index("services")]
        pairs = service_times.to_pairs(raw_services)
        yield tuple(values) + (
            pairs,
            service_times.describe(raw_services),
            "osm" if pairs else "",
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

    applied_times = load_web_service_times(connection, cursor)
    load_churchmanship(connection, cursor)

    connection.commit()
    cursor.execute("ANALYZE")
    connection.commit()
    return total


def load_web_service_times(connection, cursor):
    """Fill in service times read off parish websites.

    OpenStreetMap wins wherever it has a `service_times` tag: it is an explicit
    statement of exactly this fact, made by somebody who chose to record it. The
    website extraction is inference from prose, and only fills the silence -- of
    2,812 Anglican churches, OSM has times for 204.

    `service_source` records which, because a time nobody can trace is a time
    nobody can correct, and the cost of being wrong here is somebody standing
    outside a locked church on a Sunday morning.
    """
    if not WEB_TIMES_PATH.exists():
        return 0
    times = json.loads(WEB_TIMES_PATH.read_text()).get("times", {})
    if not times:
        return 0

    applied = 0
    for church_id, entry in times.items():
        pairs = entry.get("pairs") or ""
        if not pairs:
            continue
        cursor.execute(
            "UPDATE churches SET service_pairs = ?, service_text = ?, "
            "service_source = 'website' "
            "WHERE id = ? AND service_pairs = ''",
            (pairs, entry.get("text", ""), church_id),
        )
        applied += cursor.rowcount
    connection.commit()
    print(f"\n  Filled {applied:,} service times from parish websites.")
    return applied


def load_churchmanship(connection, cursor):
    """Apply scraped churchmanship scores, preserving any user votes.

    The scraped half is replaced wholesale; the blended columns are then
    recomputed from it plus whatever votes exist. A re-scrape therefore never
    discards a submission.
    """
    # Each source is scored separately and merged here, rather than by scoring
    # the concatenated text: a phrase appearing on both the parish website and
    # in its Wikipedia article is one fact learned twice, not two facts.
    per_source = {}
    for path, source in ((CHURCHMANSHIP_PATH, "website"),
                         (CHURCHMANSHIP_WIKI_PATH, "wikipedia")):
        if not path.exists():
            continue
        for church_id, score in json.loads(path.read_text()).get("scores", {}).items():
            score.setdefault("source", source)
            per_source.setdefault(church_id, []).append(score)

    scores = {church_id: churchmanship.merge_sources(found)
              for church_id, found in per_source.items()}
    scores = {k: v for k, v in scores.items() if v}
    if not scores:
        return 0

    known = {row[0] for row in cursor.execute("SELECT id FROM churches")}
    applied = 0
    by_source = {}
    for church_id, score in scores.items():
        if church_id not in known:
            continue
        cursor.execute(
            """INSERT INTO churchmanship
                 (church_id, scraped_ceremonial, scraped_theology, scraped_confidence,
                  scraped_source, evidence)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(church_id) DO UPDATE SET
                 scraped_ceremonial = excluded.scraped_ceremonial,
                 scraped_theology   = excluded.scraped_theology,
                 scraped_confidence = excluded.scraped_confidence,
                 scraped_source     = excluded.scraped_source,
                 evidence           = excluded.evidence""",
            (church_id, score["ceremonial"], score["theology"], score["confidence"],
             score.get("source", ""), ",".join(score.get("matched", []))),
        )
        applied += 1
        label = score.get("source", "website")
        by_source[label] = by_source.get(label, 0) + 1

    connection.commit()
    for church_id in scores:
        if church_id in known:
            recompute(connection, church_id)
    detail = ", ".join(f"{n} {name}" for name, n in sorted(by_source.items()))
    print(f"\n  Applied {applied:,} scraped churchmanship scores ({detail}).")
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


def write_static_service_times(connection):
    """Emit every service time the no-server build needs, from both sources.

    The state files carry the raw `services` tag but not the parsed pairs, which
    are derived at build time -- so on a file server the day/period filter
    matched nothing for an OSM-tagged church, and its card showed raw
    `Su 11:00` instead of "Sunday 11am". Same shape of bug as churchmanship:
    a feature that works behind the API and quietly does not on the deployed
    site.

    Both sources go in one file with `source` on each entry, so the static path
    gets the same times, the same readable text and the same provenance the API
    serves, from the same build.
    """
    rows = connection.execute(
        "SELECT id, service_pairs, service_text, service_source FROM churches "
        "WHERE service_pairs <> ''"
    ).fetchall()
    times = {
        row["id"]: {"pairs": row["service_pairs"],
                    "text": row["service_text"],
                    "source": row["service_source"] or "osm"}
        for row in rows
    }
    STATIC_TIMES_PATH.write_text(json.dumps({"times": times}, separators=(",", ":"),
                                            sort_keys=True))
    size = STATIC_TIMES_PATH.stat().st_size
    by_source = {}
    for entry in times.values():
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
    detail = ", ".join(f"{n} {name}" for name, n in sorted(by_source.items()))
    print(f"  Wrote {len(times):,} service times ({detail}) to "
          f"{STATIC_TIMES_PATH.name}, {size // 1024} KB.")
    return len(times)


def write_static_churchmanship():
    """Emit the merged readings for the no-server build.

    Without this the churchmanship feature exists only behind the API, which is
    to say not at all on the deployed site -- the state files carry no scores and
    the UI had nothing to render. The merge lives here rather than in the browser
    so both backends show the same numbers from the same code.

    Votes are deliberately absent: they need accounts, which need the server.
    What ships is the scraped prior, labelled as such.
    """
    merged = {}
    for path, source in ((CHURCHMANSHIP_PATH, "website"),
                         (CHURCHMANSHIP_WIKI_PATH, "wikipedia")):
        if not path.exists():
            continue
        for church_id, score in json.loads(path.read_text()).get("scores", {}).items():
            score.setdefault("source", source)
            merged.setdefault(church_id, []).append(score)

    scores = {}
    for church_id, found in merged.items():
        combined = churchmanship.merge_sources(found)
        if not combined:
            continue
        scores[church_id] = {
            "ceremonial": combined["ceremonial"],
            "theology": combined["theology"],
            "confidence": combined["confidence"],
            "source": combined.get("source", "website"),
            "evidence": combined.get("matched", []),
        }

    STATIC_CHURCHMANSHIP_PATH.write_text(
        json.dumps({"scores": scores}, indent=1, sort_keys=True))
    print(f"  Wrote {len(scores):,} readings to {STATIC_CHURCHMANSHIP_PATH.name} "
          f"for the static build.")


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
    write_static_churchmanship()
    write_static_service_times(connection)

    users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"\nLoaded {total:,} churches. {users} account(s) left untouched.")
    print(f"Database: {connection.execute('PRAGMA database_list').fetchone()[2]}")
    connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
