"""Church search against SQLite.

Every filter is bound as a parameter. The only strings interpolated into SQL are
column names and placeholder lists this module writes itself -- nothing derived
from a request ever reaches the statement text.
"""

import re

from db import bounding_box
from service_times import PERIODS as SERVICE_PERIODS

CHURCH_COLUMNS = [
    "id", "name", "denomination", "family", "address", "city", "state",
    "postcode", "lat", "lon", "website", "phone", "email", "services",
    "hours", "wheelchair", "hearing_loop", "toilets_wheelchair", "updated",
    "wikipedia", "wikidata", "service_pairs", "service_text",
]

SORTS = {
    "distance": "distance ASC, c.name ASC",
    "name": "c.name ASC",
    "denomination": "CASE WHEN c.denomination = '' THEN 1 ELSE 0 END, c.denomination ASC, c.name ASC",
}

MAX_LIMIT = 200
MAX_RADIUS_MILES = 250
MAX_QUERY_LENGTH = 120

# FTS5 treats bare punctuation as operators. Everything that is not a word
# character or space is dropped before the term is quoted.
_FTS_STRIP = re.compile(r"[^\w\s]+", re.UNICODE)


def fts_query(raw):
    """Turn user text into a safe FTS5 prefix query, or None if nothing is left.

    Each term is double-quoted so it is a literal string rather than syntax, and
    given a trailing `*` so "grac" finds "Grace". Terms are ANDed.
    """
    if not raw:
        return None
    cleaned = _FTS_STRIP.sub(" ", raw[:MAX_QUERY_LENGTH])
    terms = [term for term in cleaned.split() if term]
    if not terms:
        return None
    return " ".join(f'"{term}"*' for term in terms)


def _filter_clauses(params):
    """Shared WHERE fragments and their bound values."""
    clauses, values = [], []

    families = params.get("families") or []
    if families:
        clauses.append(f"c.family IN ({','.join('?' * len(families))})")
        values.extend(families)

    denominations = params.get("denominations") or []
    if denominations:
        clauses.append(f"c.denomination IN ({','.join('?' * len(denominations))})")
        values.extend(denominations)

    if params.get("state"):
        clauses.append("c.state = ?")
        values.append(params["state"])

    for flag, column in (("has_website", "website"), ("has_phone", "phone"),
                         ("has_services", "services")):
        if params.get(flag):
            clauses.append(f"c.{column} <> ''")

    if params.get("wheelchair"):
        clauses.append("c.wheelchair = 'yes'")

    if params.get("hearing_loop"):
        clauses.append("c.hearing_loop = 'yes'")

    # Service-time search over the ",D-HHMM," pairs. Selecting a day AND a
    # period asks for a service at that time *on that day* -- matching them
    # independently would answer "Sunday evening" with a church that has a
    # Sunday morning service and a Thursday evening one.
    days = params.get("service_days") or []
    periods = params.get("service_periods") or []
    hours = []
    for period in periods:
        window = SERVICE_PERIODS.get(period)
        if window:
            hours.extend(range(window[0] // 60, (window[1] - 1) // 60 + 1))

    if days and hours:
        patterns = [f"%,{day}-{hour:02d}%" for day in days for hour in hours]
    elif days:
        patterns = [f"%,{day}-%" for day in days]
    elif hours:
        # Any day, including the `x` day used when the source gave no weekday.
        patterns = [f"%-{hour:02d}%" for hour in set(hours)]
    else:
        patterns = []

    if patterns:
        clauses.append("(" + " OR ".join("c.service_pairs LIKE ?" for _ in patterns) + ")")
        values.extend(patterns)

    match = fts_query(params.get("q"))
    if match:
        clauses.append("c.rowid IN (SELECT rowid FROM churches_fts WHERE churches_fts MATCH ?)")
        values.append(match)

    return clauses, values


def search(connection, params):
    """Run a church search. Returns {"total": int, "results": [...]}.

    With lat/lon the R-tree narrows to a bounding box before any haversine runs;
    without them this is an ordinary indexed scan.
    """
    limit = max(1, min(int(params.get("limit") or 50), MAX_LIMIT))
    offset = max(0, int(params.get("offset") or 0))
    sort = params.get("sort") if params.get("sort") in SORTS else "name"

    lat, lon = params.get("lat"), params.get("lon")
    near = lat is not None and lon is not None
    radius = min(float(params.get("radius") or 25), MAX_RADIUS_MILES) if near else None

    clauses, values = _filter_clauses(params)

    if near:
        min_lat, max_lat, min_lon, max_lon = bounding_box(lat, lon, radius)
        base = """
            FROM churches c
            JOIN churches_geo g ON g.id = c.rowid
            WHERE g.min_lat >= ? AND g.max_lat <= ? AND g.min_lon >= ? AND g.max_lon <= ?
              AND haversine(?, ?, c.lat, c.lon) <= ?
        """
        box = [min_lat, max_lat, min_lon, max_lon, lat, lon, radius]
        distance_select = "haversine(?, ?, c.lat, c.lon) AS distance"
        distance_args = [lat, lon]
    else:
        base = " FROM churches c WHERE 1=1 "
        box = []
        distance_select = "NULL AS distance"
        distance_args = []
        if sort == "distance":
            sort = "name"          # nothing to measure from

    where = "".join(f" AND {clause}" for clause in clauses)

    total = connection.execute(
        f"SELECT COUNT(*) {base}{where}", box + values
    ).fetchone()[0]

    columns = ", ".join(f"c.{name}" for name in CHURCH_COLUMNS)
    # LEFT JOIN so a church with no estimate still comes back, with NULLs the
    # caller turns into "unknown" rather than a fabricated middle.
    base = base.replace("FROM churches c",
                        "FROM churches c LEFT JOIN churchmanship m ON m.church_id = c.id", 1)
    columns += (", m.ceremonial AS cm_ceremonial, m.theology AS cm_theology, "
                "m.confidence AS cm_confidence, m.votes AS cm_votes, m.source AS cm_source")
    rows = connection.execute(
        f"SELECT {columns}, {distance_select} {base}{where} "
        f"ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
        distance_args + box + values + [limit, offset],
    ).fetchall()

    return {"total": total, "results": [dict(row) for row in rows]}


def get_church(connection, church_id):
    columns = ", ".join(f"c.{name}" for name in CHURCH_COLUMNS)
    row = connection.execute(
        f"SELECT {columns}, m.ceremonial AS cm_ceremonial, m.theology AS cm_theology, "
        f"m.confidence AS cm_confidence, m.votes AS cm_votes, m.source AS cm_source, "
        f"m.evidence AS cm_evidence "
        f"FROM churches c LEFT JOIN churchmanship m ON m.church_id = c.id "
        f"WHERE c.id = ?", (church_id,)
    ).fetchone()
    return dict(row) if row else None


def get_churches(connection, church_ids):
    """Fetch many by id, preserving nothing about order -- callers sort."""
    if not church_ids:
        return []
    columns = ", ".join(CHURCH_COLUMNS)
    placeholders = ",".join("?" * len(church_ids))
    rows = connection.execute(
        f"SELECT {columns} FROM churches WHERE id IN ({placeholders})", list(church_ids)
    ).fetchall()
    return [dict(row) for row in rows]


def dataset_meta(connection):
    return {
        row["key"]: row["value"]
        for row in connection.execute("SELECT key, value FROM dataset_meta")
    }


def facets(connection, family_labels, state_names):
    """Counts for the filter UI: families, the denominations inside each, and states.

    The data is read-only at runtime, so the caller caches this for the process
    lifetime rather than recomputing per request.
    """
    families = []
    for row in connection.execute(
        "SELECT family, COUNT(*) AS n FROM churches GROUP BY family ORDER BY n DESC"
    ):
        families.append({
            "key": row["family"],
            "label": family_labels.get(row["family"], row["family"]),
            "count": row["n"],
        })
    # "Unspecified" is the largest bucket by far; leading the filter list with it
    # would bury every real choice.
    families.sort(key=lambda item: (item["key"] in ("unknown", "other"), -item["count"]))

    denominations = {}
    for row in connection.execute(
        """SELECT family, denomination, COUNT(*) AS n FROM churches
           WHERE denomination <> '' GROUP BY family, denomination ORDER BY n DESC"""
    ):
        denominations.setdefault(row["family"], []).append(
            {"label": row["denomination"], "count": row["n"]}
        )

    states = []
    for row in connection.execute(
        "SELECT state, COUNT(*) AS n FROM churches GROUP BY state ORDER BY state"
    ):
        states.append({
            "code": row["state"],
            "name": state_names.get(row["state"], row["state"]),
            "count": row["n"],
        })

    total = connection.execute("SELECT COUNT(*) FROM churches").fetchone()[0]
    return {"families": families, "denominations": denominations,
            "states": states, "total": total}
