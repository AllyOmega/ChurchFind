"""Tests for the API, weighted towards the parts that would hurt if they broke.

Runs against a throwaway database seeded with a handful of churches, so it does
not need the 59 MB build and does not touch a real one.

    cd server && python -m pytest test_api.py -q
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Point the app at a scratch database and relax the cookie flag before importing
# it -- both are read at import/startup time.
_TEMP_DIR = tempfile.mkdtemp(prefix="churchfind-test-")
os.environ["CHURCHFIND_DB"] = str(Path(_TEMP_DIR) / "test.db")
os.environ["CHURCHFIND_INSECURE_COOKIES"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import auth  # noqa: E402

GOOD_PASSWORD = "correct-horse-battery-staple"

CHURCHES = [
    ("n1", "First Baptist Church", "Southern Baptist", "baptist", "1 Main St",
     "Denver", "CO", "80203", 39.7392, -104.9848, "https://example.org", "+1 303-555-0100",
     "", "Su 10:00", "", "yes"),
    ("n2", "Saint Mary Catholic Church", "Roman Catholic", "catholic", "2 Oak Ave",
     "Denver", "CO", "80205", 39.7500, -104.9900, "", "", "", "", "", ""),
    ("n3", "Grace Lutheran Church", "Lutheran", "lutheran", "3 Elm St",
     "Boulder", "CO", "80301", 40.0150, -105.2705, "https://example.com", "", "", "", "", "no"),
    ("n4", "Trinity Church", "", "unknown", "", "Kansas City", "MO", "",
     39.1000, -94.5786, "", "", "", "", "", ""),
    ("n5", "Bethel Church of Christ", "Church of Christ", "restorationist", "5 Pine Rd",
     "Kansas City", "KS", "66101", 39.1141, -94.6275, "", "+1 913-555-0177", "", "", "", ""),
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as test_client:
        connection = app_module._connection
        columns = ",".join([
            "id", "name", "denomination", "family", "address", "city", "state",
            "postcode", "lat", "lon", "website", "phone", "email", "services",
            "hours", "wheelchair",
        ])
        connection.executemany(
            f"INSERT OR REPLACE INTO churches ({columns}) VALUES ({','.join('?' * 16)})",
            CHURCHES,
        )
        connection.execute("DELETE FROM churches_geo")
        connection.execute(
            "INSERT INTO churches_geo (id, min_lat, max_lat, min_lon, max_lon) "
            "SELECT rowid, lat, lat, lon, lon FROM churches"
        )
        connection.execute("INSERT INTO churches_fts(churches_fts) VALUES('rebuild')")
        connection.commit()
        app_module._facets_cache = None
        yield test_client


@pytest.fixture(autouse=True)
def clean_accounts(client):
    """Every test starts with no users, sessions or throttle history."""
    connection = app_module._connection
    for table in ("saved_churches", "correction_reports", "sessions", "auth_attempts", "users"):
        connection.execute(f"DELETE FROM {table}")
    connection.commit()
    client.cookies.clear()


def register(client, email="person@example.org", password=GOOD_PASSWORD, **extra):
    return client.post("/api/auth/register",
                       json={"email": email, "password": password, **extra})


def csrf(client):
    return client.cookies.get(auth.CSRF_COOKIE)


# ------------------------------------------------------------------- churches

def test_health_and_meta(client):
    assert client.get("/api/health").json()["ok"] is True
    meta = client.get("/api/meta").json()
    families = {f["key"]: f["count"] for f in meta["families"]}
    assert families["baptist"] == 1 and families["catholic"] == 1
    # Denominations are grouped under their family for the two-level filter.
    assert meta["denominations"]["baptist"][0]["label"] == "Southern Baptist"


def test_radius_search_sorts_by_distance(client):
    body = client.get("/api/churches",
                      params={"lat": 39.7392, "lon": -104.9848, "radius": 20}).json()
    names = [c["name"] for c in body["results"]]
    assert names[0] == "First Baptist Church"          # the origin sits on it
    distances = [c["distance"] for c in body["results"]]
    assert distances == sorted(distances)
    assert "Grace Lutheran Church" not in names        # Boulder is ~24 miles out


def test_radius_excludes_beyond_edge(client):
    near = client.get("/api/churches", params={"lat": 39.7392, "lon": -104.9848, "radius": 5})
    far = client.get("/api/churches", params={"lat": 39.7392, "lon": -104.9848, "radius": 50})
    assert near.json()["total"] == 2 and far.json()["total"] == 3


def test_filter_by_family_and_denomination(client):
    by_family = client.get("/api/churches", params={"state": "CO", "family": "baptist"}).json()
    assert [c["name"] for c in by_family["results"]] == ["First Baptist Church"]

    by_denomination = client.get(
        "/api/churches", params={"state": "CO", "denomination": "Roman Catholic"}
    ).json()
    assert [c["name"] for c in by_denomination["results"]] == ["Saint Mary Catholic Church"]

    both = client.get("/api/churches", params={"family": "baptist,catholic"}).json()
    assert both["total"] == 2


def test_toggles(client):
    assert client.get("/api/churches", params={"has_website": True}).json()["total"] == 2
    assert client.get("/api/churches", params={"has_phone": True}).json()["total"] == 2
    assert client.get("/api/churches", params={"wheelchair": True}).json()["total"] == 1
    assert client.get("/api/churches", params={"has_services": True}).json()["total"] == 1


def test_name_search(client):
    assert client.get("/api/churches", params={"q": "grace"}).json()["total"] == 1
    assert client.get("/api/churches", params={"q": "gra"}).json()["total"] == 1   # prefix
    assert client.get("/api/churches", params={"q": "church"}).json()["total"] == 5


@pytest.mark.parametrize("hostile", [
    "'; DROP TABLE churches; --",
    '" OR 1=1 --',
    "church*)(",
    "NEAR/2",
    "^",
    "a" * 500,
])
def test_search_survives_hostile_input(client, hostile):
    """FTS5 has its own query syntax; unescaped punctuation is a parse error at
    best and an operator at worst. Nothing here may 500 or drop a table."""
    response = client.get("/api/churches", params={"q": hostile})
    assert response.status_code == 200
    assert client.get("/api/health").json()["churches"] >= 0
    assert app_module._connection.execute("SELECT COUNT(*) FROM churches").fetchone()[0] == 5


def test_rejects_bad_geo_params(client):
    assert client.get("/api/churches", params={"lat": 39.7}).status_code == 422
    assert client.get("/api/churches", params={"lat": 999, "lon": 0}).status_code == 422
    assert client.get("/api/churches", params={"state": "ZZ"}).status_code == 422


def test_limit_is_capped(client):
    body = client.get("/api/churches", params={"limit": 100000}).json()
    assert len(body["results"]) <= 200


# ----------------------------------------------------------------- registration

def test_register_sets_httponly_session(client):
    response = register(client)
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "person@example.org"

    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(auth.SESSION_COOKIE))
    assert "httponly" in session_cookie.lower() and "samesite=lax" in session_cookie.lower()
    # The CSRF cookie must NOT be HttpOnly -- the page has to read it.
    csrf_cookie = next(c for c in cookies if c.startswith(auth.CSRF_COOKIE))
    assert "httponly" not in csrf_cookie.lower()


@pytest.mark.parametrize("password", [
    "short", "password123", "12345678901", "", "aaaaaaaaaaaa", "1234567890123456",
])
def test_weak_passwords_rejected(client, password):
    assert register(client, password=password).status_code == 422


@pytest.mark.parametrize("email", ["not-an-email", "@example.org", "a@b", "", "a@b.c@d.e"])
def test_bad_emails_rejected(client, email):
    assert register(client, email=email).status_code == 422


def test_duplicate_email_rejected_case_insensitively(client):
    assert register(client, email="Person@Example.org").status_code == 200
    assert register(client, email="person@example.ORG").status_code == 409


def test_password_is_not_stored_in_the_clear(client):
    register(client)
    row = app_module._connection.execute("SELECT password_hash FROM users").fetchone()
    assert GOOD_PASSWORD not in row["password_hash"]
    assert row["password_hash"].startswith("$argon2id$")


def test_session_token_is_not_stored(client):
    register(client)
    raw = client.cookies.get(auth.SESSION_COOKIE)
    stored = app_module._connection.execute("SELECT token_hash FROM sessions").fetchone()
    # The database holds only the digest, so reading it cannot mint a cookie.
    assert stored["token_hash"] != raw
    assert stored["token_hash"] == auth.token_hash(raw)


# ------------------------------------------------------------------------ login

def test_login_round_trip(client):
    register(client)
    client.cookies.clear()
    assert client.get("/api/auth/me").json()["user"] is None

    response = client.post("/api/auth/login",
                           json={"email": "person@example.org", "password": GOOD_PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == "person@example.org"


def test_login_does_not_reveal_whether_the_account_exists(client):
    register(client)
    client.cookies.clear()
    missing = client.post("/api/auth/login",
                          json={"email": "nobody@example.org", "password": GOOD_PASSWORD})
    wrong = client.post("/api/auth/login",
                        json={"email": "person@example.org", "password": "wrong-password-here"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["detail"] == wrong.json()["detail"]


def test_login_throttled_per_email(client):
    register(client)
    client.cookies.clear()
    codes = [
        client.post("/api/auth/login",
                    json={"email": "person@example.org", "password": "wrong-password-here"}).status_code
        for _ in range(auth.LOGIN_MAX_PER_EMAIL + 2)
    ]
    assert 429 in codes
    # Even the right password is refused while the account is throttled.
    assert client.post("/api/auth/login",
                       json={"email": "person@example.org",
                             "password": GOOD_PASSWORD}).status_code == 429


def test_successful_login_clears_the_throttle(client):
    register(client)
    client.cookies.clear()
    for _ in range(3):
        client.post("/api/auth/login",
                    json={"email": "person@example.org", "password": "wrong-password-here"})
    assert client.post("/api/auth/login",
                       json={"email": "person@example.org",
                             "password": GOOD_PASSWORD}).status_code == 200


# ------------------------------------------------------------------------- CSRF

def test_state_changing_requests_need_the_csrf_header(client):
    register(client)
    # The session cookie alone is not enough -- this is the cross-site case.
    assert client.post("/api/saved", json={"church_id": "n1"}).status_code == 403
    assert client.post("/api/saved", json={"church_id": "n1"},
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/saved", json={"church_id": "n1"},
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 200


def test_reads_do_not_need_csrf(client):
    register(client)
    assert client.get("/api/saved").status_code == 200


# -------------------------------------------------------------- account data

def test_save_and_unsave(client):
    register(client)
    token = csrf(client)

    client.post("/api/saved", json={"church_id": "n1", "note": "visited"},
                headers={"X-CSRF-Token": token})
    saved = client.get("/api/saved").json()
    assert saved["total"] == 1
    assert saved["results"][0]["name"] == "First Baptist Church"
    assert saved["results"][0]["note"] == "visited"

    # Saving twice updates the note rather than erroring.
    client.post("/api/saved", json={"church_id": "n1", "note": "again"},
                headers={"X-CSRF-Token": token})
    assert client.get("/api/saved").json()["results"][0]["note"] == "again"

    client.delete("/api/saved/n1", headers={"X-CSRF-Token": token})
    assert client.get("/api/saved").json()["total"] == 0


def test_saving_an_unknown_church_404s(client):
    register(client)
    assert client.post("/api/saved", json={"church_id": "nope"},
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 404


def test_users_cannot_see_each_other_s_saves(client):
    register(client, email="first@example.org")
    client.post("/api/saved", json={"church_id": "n1"}, headers={"X-CSRF-Token": csrf(client)})
    client.cookies.clear()

    register(client, email="second@example.org")
    assert client.get("/api/saved").json()["total"] == 0
    # And deleting the other user's row is a no-op, not an error that leaks its existence.
    client.delete("/api/saved/n1", headers={"X-CSRF-Token": csrf(client)})

    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "first@example.org", "password": GOOD_PASSWORD})
    assert client.get("/api/saved").json()["total"] == 1


def test_anonymous_requests_are_refused(client):
    assert client.get("/api/saved").status_code == 401
    for method, path in (("post", "/api/saved"), ("put", "/api/me/home"),
                         ("post", "/api/reports")):
        response = getattr(client, method)(path, json={})
        assert response.status_code == 401, path


def test_home_location(client):
    register(client)
    client.put("/api/me/home", json={"lat": 39.7, "lon": -104.9, "label": "Denver"},
               headers={"X-CSRF-Token": csrf(client)})
    assert client.get("/api/auth/me").json()["user"]["home"]["label"] == "Denver"


def test_home_location_rejects_impossible_coordinates(client):
    register(client)
    assert client.put("/api/me/home", json={"lat": 200, "lon": 0},
                      headers={"X-CSRF-Token": csrf(client)}).status_code == 422


def test_correction_report(client):
    register(client)
    response = client.post("/api/reports",
                           json={"church_id": "n1", "field": "phone", "suggestion": "disconnected"},
                           headers={"X-CSRF-Token": csrf(client)})
    assert response.status_code == 200
    assert "openstreetmap.org" in response.json()["note"]
    assert app_module._connection.execute(
        "SELECT COUNT(*) FROM correction_reports").fetchone()[0] == 1


# -------------------------------------------------------------------- sessions

def test_logout_destroys_the_session(client):
    register(client)
    raw = client.cookies.get(auth.SESSION_COOKIE)
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf(client)})
    assert client.get("/api/auth/me").json()["user"] is None
    # Replaying the old cookie must not work either.
    client.cookies.set(auth.SESSION_COOKIE, raw)
    assert client.get("/api/auth/me").json()["user"] is None


def test_changing_password_signs_out_other_devices(client):
    register(client)
    first_session = client.cookies.get(auth.SESSION_COOKIE)

    response = client.post("/api/auth/password",
                           json={"current_password": GOOD_PASSWORD,
                                 "new_password": "another-long-passphrase"},
                           headers={"X-CSRF-Token": csrf(client)})
    assert response.json()["signedOutEverywhere"] is True

    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, first_session)
    assert client.get("/api/auth/me").json()["user"] is None

    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"email": "person@example.org",
                             "password": "another-long-passphrase"}).status_code == 200


def test_password_change_requires_the_current_one(client):
    register(client)
    assert client.post("/api/auth/password",
                       json={"current_password": "not-it-at-all",
                             "new_password": "another-long-passphrase"},
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 403


def test_expired_session_is_rejected_and_removed(client):
    register(client)
    connection = app_module._connection
    connection.execute("UPDATE sessions SET expires_at = ?",
                       (auth.iso(auth.now().replace(year=2000)),))
    connection.commit()
    assert client.get("/api/auth/me").json()["user"] is None
    assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_forged_session_cookie_is_rejected(client):
    client.cookies.set(auth.SESSION_COOKIE, "a" * 43)
    assert client.get("/api/auth/me").json()["user"] is None


def test_security_headers_present(client):
    headers = client.get("/api/health").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
