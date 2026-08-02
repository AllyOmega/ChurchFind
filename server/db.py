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


def init_schema(connection):
    connection.executescript(SCHEMA.read_text())
    connection.commit()


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
