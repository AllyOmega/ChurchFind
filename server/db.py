"""SQLite connection handling and the haversine SQL function.

One database file holds both halves: the scraped churches and the account data.
Keeping them together means a saved church is a real foreign key rather than an
identifier the application has to police itself.
"""

import math
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "server" / "churchfind.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

EARTH_RADIUS_MILES = 3958.8


def database_path():
    """Env override exists so tests and deployments don't share a file."""
    return Path(os.environ.get("CHURCHFIND_DB", DEFAULT_DB))


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle miles. Registered into SQLite so ORDER BY can use it."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(a)))


def connect(path=None, readonly=False):
    """Open a connection with the pragmas and functions this app expects."""
    target = Path(path) if path else database_path()

    if readonly:
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True, check_same_thread=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, check_same_thread=False)

    connection.row_factory = sqlite3.Row
    # Enforced per-connection in SQLite, not stored in the file.
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    connection.create_function("haversine", 4, _haversine, deterministic=True)
    return connection


# Columns added to `churches` after the first release. `CREATE TABLE IF NOT
# EXISTS` is a no-op on an existing table, so a schema.sql that grows a column
# silently does nothing -- and then the index referencing it fails. These are
# applied before the schema script runs.
_ADDED_USER_COLUMNS = [
    ("is_moderator", "INTEGER NOT NULL DEFAULT 0"),
]

_ADDED_CHURCHMANSHIP_COLUMNS = [
    ("scraped_source", "TEXT NOT NULL DEFAULT ''"),
]

_ADDED_CHURCH_COLUMNS = [
    ("hearing_loop", "TEXT NOT NULL DEFAULT ''"),
    ("toilets_wheelchair", "TEXT NOT NULL DEFAULT ''"),
    ("updated", "TEXT NOT NULL DEFAULT ''"),
    ("wikipedia", "TEXT NOT NULL DEFAULT ''"),
    ("wikidata", "TEXT NOT NULL DEFAULT ''"),
    ("service_pairs", "TEXT NOT NULL DEFAULT ''"),
    ("service_text", "TEXT NOT NULL DEFAULT ''"),
    ("service_source", "TEXT NOT NULL DEFAULT ''"),
    ("service_checked", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate_table(connection, table, columns):
    """Add any column the current schema expects but this file predates."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    present = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    added = []
    for column, definition in columns:
        if column not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.append(f"{table}.{column}")
    if added:
        connection.commit()
    return added


def _migrate_churches(connection):
    """Add any column the current schema expects but this file predates.

    Only ever adds -- nothing here drops or rewrites data. The church rows are a
    rebuildable cache, but saved_churches and correction_reports have foreign
    keys into them, so recreating the table wholesale is not an option.
    """
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='churches'"
    ).fetchone()
    if not exists:
        return []

    present = {row["name"] for row in connection.execute("PRAGMA table_info(churches)")}
    added = []
    for column, definition in _ADDED_CHURCH_COLUMNS:
        if column not in present:
            connection.execute(f"ALTER TABLE churches ADD COLUMN {column} {definition}")
            added.append(column)
    if added:
        connection.commit()
    return added


def init_schema(connection):
    added = _migrate_churches(connection)
    added += _migrate_table(connection, "users", _ADDED_USER_COLUMNS)
    added += _migrate_table(connection, "churchmanship", _ADDED_CHURCHMANSHIP_COLUMNS)
    connection.executescript(SCHEMA.read_text())
    connection.commit()
    return added


def bounding_box(lat, lon, radius_miles):
    """Degree box enclosing a radius, for narrowing with the R-tree.

    Latitude is a flat 69 miles per degree. Longitude shrinks towards the poles,
    and the cosine is clamped so a search near a pole widens the box rather than
    dividing by zero.
    """
    lat_delta = radius_miles / 69.0
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    lon_delta = radius_miles / (69.0 * cos_lat)
    return (
        max(lat - lat_delta, -90.0), min(lat + lat_delta, 90.0),
        max(lon - lon_delta, -180.0), min(lon + lon_delta, 180.0),
    )
