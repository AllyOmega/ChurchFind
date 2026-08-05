"""Tests for the API, weighted towards the parts that would hurt if they broke.

Runs against a throwaway database seeded with a handful of churches, so it does
not need the 59 MB build and does not touch a real one.

    cd server && python -m pytest test_api.py -q
"""

import json
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
        ])   # later columns (accessibility, service times) default to empty
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
    for table in ("reviews", "saved_churches", "correction_reports", "sessions",
                  "auth_attempts", "users"):
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


# ------------------------------------------------------------------ schema drift

def test_base_schema_and_migration_list_agree():
    """A column added to the migration list but not to CREATE TABLE gives
    upgraded databases a column that fresh installs lack -- which is exactly the
    bug this test was written after hitting."""
    import db as db_module

    connection = app_module._connection
    present = {row["name"] for row in connection.execute("PRAGMA table_info(churches)")}
    for column, _ in db_module._ADDED_CHURCH_COLUMNS:
        assert column in present, f"{column} is in the migration list but not in schema.sql"

    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    for column, _ in db_module._ADDED_USER_COLUMNS:
        assert column in user_columns, f"{column} is in the migration list but not in schema.sql"

    cm_columns = {row["name"] for row in
                  connection.execute("PRAGMA table_info(churchmanship)")}
    for column, _ in db_module._ADDED_CHURCHMANSHIP_COLUMNS:
        assert column in cm_columns, f"{column} is in the migration list but not in schema.sql"


def test_build_columns_match_the_scraper_fields():
    """build_db reads state files positionally by name. A field added to the
    scraper but not to build_db's COLUMNS is silently dropped on the way into
    the database, and nothing else notices."""
    import build_db
    import scrape_churches

    assert set(build_db.COLUMNS) <= set(scrape_churches.FIELDS)
    missing = set(scrape_churches.FIELDS) - set(build_db.COLUMNS)
    assert not missing, f"scraper writes {missing} but build_db never reads them"


def test_query_columns_all_exist():
    """queries.CHURCH_COLUMNS is interpolated into SQL; every name must be real."""
    import queries as queries_module

    present = {row["name"] for row in
               app_module._connection.execute("PRAGMA table_info(churches)")}
    assert set(queries_module.CHURCH_COLUMNS) <= present


# ---------------------------------------------------------------------- reviews

@pytest.fixture
def stub_moderation(monkeypatch):
    """Replace the Claude call with a canned verdict. test_moderation.py covers
    the classifier itself; these tests cover what the API does with its answer."""
    import moderation as moderation_module

    calls = []

    def make(status):
        def fake(body, church_name, rating, client=None):
            calls.append({"body": body, "church": church_name, "rating": rating})
            return {"status": status, "verdict": status, "categories": [],
                    "reason": "stubbed", "confidence": "high",
                    "model": "stub", "usage": None}
        monkeypatch.setattr(moderation_module, "moderate", fake)
        return calls
    return make


def post_review(client, status="approved", church="n1", body="A thoughtful review.", rating=4):
    return client.post("/api/reviews", json={"church_id": church, "rating": rating, "body": body},
                       headers={"X-CSRF-Token": csrf(client)})


def test_approved_review_is_published(client, stub_moderation):
    stub_moderation("approved")
    register(client)
    assert post_review(client).json()["status"] == "approved"

    body = client.get("/api/churches/n1/reviews").json()
    assert body["total"] == 1 and body["average"] == 4.0
    assert body["results"][0]["body"] == "A thoughtful review."


@pytest.mark.parametrize("status", ["pending", "escalated", "rejected"])
def test_unapproved_reviews_are_invisible_to_everyone_else(client, stub_moderation, status):
    stub_moderation(status)
    register(client, email="author@example.org")
    post_review(client, status)

    # The author still sees their own, with its status -- "held for review" beats
    # a review that silently vanishes.
    own = client.get("/api/churches/n1/reviews").json()
    assert own["total"] == 0                    # not counted as published
    assert len(own["results"]) == 1 and own["results"][0]["mine"] is True

    client.cookies.clear()
    assert client.get("/api/churches/n1/reviews").json()["results"] == []

    register(client, email="other@example.org")
    assert client.get("/api/churches/n1/reviews").json()["results"] == []


def test_moderation_failure_does_not_publish(client, monkeypatch):
    """With no API key the real moderate() holds the review; it must not appear."""
    import moderation as moderation_module
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert moderation_module.available() is False

    register(client)
    assert post_review(client).json()["status"] == "pending"
    client.cookies.clear()
    assert client.get("/api/churches/n1/reviews").json()["total"] == 0


def test_review_requires_sign_in_and_csrf(client, stub_moderation):
    stub_moderation("approved")
    assert client.post("/api/reviews", json={"church_id": "n1", "rating": 4, "body": "x"}).status_code == 401
    register(client)
    assert client.post("/api/reviews",
                       json={"church_id": "n1", "rating": 4, "body": "x"}).status_code == 403


@pytest.mark.parametrize("payload", [
    {"church_id": "n1", "rating": 0, "body": "x"},
    {"church_id": "n1", "rating": 6, "body": "x"},
    {"church_id": "n1", "rating": 3, "body": ""},
    {"church_id": "n1", "rating": 3, "body": "x" * 99999},
])
def test_invalid_reviews_rejected(client, stub_moderation, payload):
    stub_moderation("approved")
    register(client)
    assert client.post("/api/reviews", json=payload,
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 422


def test_review_of_unknown_church_404s(client, stub_moderation):
    stub_moderation("approved")
    register(client)
    assert post_review(client, church="nope").status_code == 404


def test_second_review_replaces_the_first_and_is_re_moderated(client, stub_moderation):
    calls = stub_moderation("approved")
    register(client)
    post_review(client, body="First take.")
    post_review(client, body="Changed my mind.")

    body = client.get("/api/churches/n1/reviews").json()
    assert body["total"] == 1                      # one per user per church
    assert body["results"][0]["body"] == "Changed my mind."
    assert len(calls) == 2                          # the edit went through moderation too


def test_author_can_delete_their_own_review_but_not_anothers(client, stub_moderation):
    stub_moderation("approved")
    register(client, email="first@example.org")
    post_review(client)
    review_id = client.get("/api/churches/n1/reviews").json()["results"][0]["id"]

    client.cookies.clear()
    register(client, email="second@example.org")
    assert client.delete(f"/api/reviews/{review_id}",
                         headers={"X-CSRF-Token": csrf(client)}).status_code == 404

    client.cookies.clear()
    client.post("/api/auth/login", json={"email": "first@example.org", "password": GOOD_PASSWORD})
    assert client.delete(f"/api/reviews/{review_id}",
                         headers={"X-CSRF-Token": csrf(client)}).status_code == 200
    assert client.get("/api/churches/n1/reviews").json()["total"] == 0


def test_average_ignores_unapproved(client, stub_moderation):
    stub_moderation("approved")
    register(client, email="a@example.org")
    post_review(client, rating=5)
    client.cookies.clear()

    stub_moderation("escalated")
    register(client, email="b@example.org")
    post_review(client, rating=1)
    client.cookies.clear()

    assert client.get("/api/churches/n1/reviews").json()["average"] == 5.0


# ------------------------------------------------------------- moderation queue

def make_moderator(client, email="mod@example.org"):
    register(client, email=email)
    app_module._connection.execute(
        "UPDATE users SET is_moderator = 1 WHERE email = ?", (email,))
    app_module._connection.commit()


def test_queue_requires_a_moderator(client):
    assert client.get("/api/moderation/queue").status_code == 401
    register(client)
    assert client.get("/api/moderation/queue").status_code == 403


def test_queue_lists_held_reviews_with_the_model_s_reasoning(client, stub_moderation):
    stub_moderation("escalated")
    register(client, email="author@example.org")
    post_review(client, body="Something the classifier was unsure about.")
    client.cookies.clear()

    make_moderator(client)
    body = client.get("/api/moderation/queue").json()
    assert body["total"] == 1
    entry = body["results"][0]
    assert entry["status"] == "escalated"
    assert entry["moderation"]["reason"] == "stubbed"
    assert entry["authorEmail"] == "author@example.org"


def test_moderator_can_publish_a_held_review(client, stub_moderation):
    stub_moderation("escalated")
    register(client, email="author@example.org")
    post_review(client)
    client.cookies.clear()

    make_moderator(client)
    review_id = client.get("/api/moderation/queue").json()["results"][0]["id"]
    assert client.post(f"/api/moderation/reviews/{review_id}", json={"status": "approved"},
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 200

    client.cookies.clear()
    assert client.get("/api/churches/n1/reviews").json()["total"] == 1

    # The human decision is recorded separately from the model's.
    row = app_module._connection.execute(
        "SELECT decided_by, mod_verdict FROM reviews WHERE id = ?", (review_id,)).fetchone()
    assert row["decided_by"] is not None and row["mod_verdict"] == "escalated"


def test_moderator_cannot_set_an_arbitrary_status(client, stub_moderation):
    stub_moderation("escalated")
    register(client, email="author@example.org")
    post_review(client)
    client.cookies.clear()
    make_moderator(client)
    review_id = client.get("/api/moderation/queue").json()["results"][0]["id"]
    assert client.post(f"/api/moderation/reviews/{review_id}", json={"status": "deleted"},
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 422


# ---------------------------------------------------------------- churchmanship

@pytest.fixture
def anglican_church(client):
    """n2 is Catholic in the base fixture; make one Anglican to vote on."""
    app_module._connection.execute(
        "UPDATE churches SET family = 'anglican', denomination = 'Episcopal' WHERE id = 'n2'")
    app_module._connection.execute("DELETE FROM churchmanship")
    app_module._connection.execute("DELETE FROM churchmanship_votes")
    app_module._connection.commit()
    return "n2"


def vote(client, church_id, ceremonial, theology):
    return client.post("/api/churchmanship",
                       json={"church_id": church_id, "ceremonial": ceremonial,
                             "theology": theology},
                       headers={"X-CSRF-Token": csrf(client)})


def test_unknown_church_is_reported_as_unknown_not_middle(client, anglican_church):
    """Zero and no-data are different answers; conflating them invents a claim."""
    body = client.get(f"/api/churchmanship/{anglican_church}").json()
    assert body["known"] is False
    assert "ceremonial" not in body or body.get("ceremonial") is None


def test_a_single_vote_creates_an_estimate(client, anglican_church):
    register(client)
    assert vote(client, anglican_church, 0.5, 1.0).status_code == 200

    body = client.get(f"/api/churchmanship/{anglican_church}").json()
    assert body["known"] is True
    assert body["votes"] == 1
    assert body["ceremonialLabel"] == "High church"
    assert body["theologyLabel"] == "Anglo-Catholic"
    assert body["mine"] == {"ceremonial": 0.5, "theology": 1.0}


def test_votes_are_one_per_user_and_updatable(client, anglican_church):
    register(client)
    vote(client, anglican_church, 1.0, 1.0)
    vote(client, anglican_church, -1.0, -1.0)
    body = client.get(f"/api/churchmanship/{anglican_church}").json()
    assert body["votes"] == 1
    assert body["ceremonial"] == -1.0


def test_disagreement_lowers_confidence(client, anglican_church):
    """Three people who contradict each other describe a parish that is hard to
    place. The meter has to get less sure, not average them into a confident
    middle."""
    agreeing = []
    for index, pair in enumerate([(0.9, 0.9), (1.0, 0.8), (0.8, 1.0)]):
        client.cookies.clear()
        register(client, email=f"agree{index}@example.org")
        vote(client, anglican_church, *pair)
        agreeing.append(client.get(f"/api/churchmanship/{anglican_church}").json())
    confident = agreeing[-1]["confidence"]

    app_module._connection.execute("DELETE FROM churchmanship_votes")
    app_module._connection.execute("DELETE FROM churchmanship")
    app_module._connection.commit()

    for index, pair in enumerate([(1.0, 1.0), (-1.0, -1.0), (0.0, 0.0)]):
        client.cookies.clear()
        register(client, email=f"differ{index}@example.org")
        vote(client, anglican_church, *pair)
    conflicted = client.get(f"/api/churchmanship/{anglican_church}").json()

    assert conflicted["confidence"] < confident
    assert conflicted["votes"] == 3


def test_withdrawing_the_only_vote_returns_to_unknown(client, anglican_church):
    register(client)
    vote(client, anglican_church, 0.5, 0.5)
    assert client.get(f"/api/churchmanship/{anglican_church}").json()["known"] is True

    client.delete(f"/api/churchmanship/{anglican_church}",
                  headers={"X-CSRF-Token": csrf(client)})
    assert client.get(f"/api/churchmanship/{anglican_church}").json()["known"] is False


def test_only_anglican_churches_can_be_scored(client):
    """n1 is Baptist. Churchmanship is an Anglican concept and applying it
    elsewhere would be a category error, not just noise."""
    register(client)
    assert vote(client, "n1", 0.5, 0.5).status_code == 422


def test_vote_requires_sign_in_and_csrf(client, anglican_church):
    assert client.post("/api/churchmanship",
                       json={"church_id": anglican_church, "ceremonial": 0, "theology": 0}
                       ).status_code == 401
    register(client)
    assert client.post("/api/churchmanship",
                       json={"church_id": anglican_church, "ceremonial": 0, "theology": 0}
                       ).status_code == 403


@pytest.mark.parametrize("payload", [
    {"ceremonial": 2, "theology": 0}, {"ceremonial": 0, "theology": -5},
])
def test_out_of_range_votes_rejected(client, anglican_church, payload):
    register(client)
    payload = dict(payload, church_id=anglican_church)
    assert client.post("/api/churchmanship", json=payload,
                       headers={"X-CSRF-Token": csrf(client)}).status_code == 422


# ------------------------------------------------- the static build's churchmanship

def test_static_churchmanship_is_the_same_merge_the_database_gets():
    """The deployed site is the static build, and it reads its churchmanship from
    data/churchmanship-merged.json rather than from a LEFT JOIN. Both come from
    the same two score files, and both must come from the same merge -- otherwise
    a parish reads one way with a server running and another way without, which
    is exactly how the feature came to be invisible on the live site.

    This recomputes the merge from the source files and compares, rather than
    reading the database: the test fixture holds five synthetic churches and no
    readings at all, so a database comparison would pass by having nothing to say.
    """
    import build_db
    import churchmanship

    path = build_db.STATIC_CHURCHMANSHIP_PATH
    if not path.exists():
        pytest.skip("run server/build_db.py to generate the static readings")

    per_source = {}
    for source_path, source in ((build_db.CHURCHMANSHIP_PATH, "website"),
                                (build_db.CHURCHMANSHIP_WIKI_PATH, "wikipedia")):
        if not source_path.exists():
            continue
        for church_id, score in json.loads(source_path.read_text())["scores"].items():
            score.setdefault("source", source)
            per_source.setdefault(church_id, []).append(score)

    published = json.loads(path.read_text())["scores"]
    assert published, "the static build would show no churchmanship at all"

    for church_id, found in per_source.items():
        expected = churchmanship.merge_sources(found)
        if not expected:
            assert church_id not in published
            continue
        actual = published[church_id]
        assert actual["ceremonial"] == pytest.approx(expected["ceremonial"])
        assert actual["theology"] == pytest.approx(expected["theology"])
        assert actual["confidence"] == pytest.approx(expected["confidence"])
        assert actual["source"] == expected["source"]

    assert set(published) == {k for k, v in per_source.items()
                              if churchmanship.merge_sources(v)}


def test_every_static_reading_carries_its_provenance():
    """A number with no source is the failure mode this feature exists to avoid."""
    import build_db

    path = build_db.STATIC_CHURCHMANSHIP_PATH
    if not path.exists():
        pytest.skip("run server/build_db.py to generate the static readings")

    for church_id, reading in json.loads(path.read_text())["scores"].items():
        assert reading["source"], f"{church_id} has a reading with no source"
        assert reading["confidence"] > 0, f"{church_id} has a reading with no confidence"
        assert reading["evidence"], f"{church_id} has a reading with no evidence"


# ---------------------------------------------------- churchmanship filtering

@pytest.fixture
def rated_churches(client):
    """Two Anglican parishes with opposite readings, plus one with none."""
    connection = app_module._connection
    connection.executemany(
        "INSERT OR REPLACE INTO churches "
        "(id, name, denomination, family, state, lat, lon) VALUES (?,?,?,?,?,?,?)",
        [("cm1", "Saint Mary the Virgin", "Episcopal", "anglican", "CO", 39.74, -104.98),
         ("cm2", "Christ Church Plain", "Episcopal", "anglican", "CO", 39.75, -104.99),
         ("cm3", "Saint Nobody", "Episcopal", "anglican", "CO", 39.76, -104.97)],
    )
    connection.executemany(
        "INSERT OR REPLACE INTO churchmanship "
        "(church_id, ceremonial, theology, confidence) VALUES (?,?,?,?)",
        [("cm1", 0.8, 0.7, 0.5), ("cm2", -0.8, -0.7, 0.5)],
    )
    connection.commit()
    yield
    connection.execute("DELETE FROM churchmanship WHERE church_id IN ('cm1','cm2')")
    connection.execute("DELETE FROM churches WHERE id IN ('cm1','cm2','cm3')")
    connection.commit()


def test_churchmanship_bands_select_the_right_parishes(client, rated_churches):
    high = client.get("/api/churches", params={"state": "CO", "churchmanship": "high"}).json()
    assert [c["id"] for c in high["results"]] == ["cm1"]

    low = client.get("/api/churches", params={"state": "CO", "churchmanship": "low"}).json()
    assert [c["id"] for c in low["results"]] == ["cm2"]

    both = client.get("/api/churches",
                      params={"state": "CO", "churchmanship": "high,low"}).json()
    assert {c["id"] for c in both["results"]} == {"cm1", "cm2"}


def test_an_unrated_parish_never_matches_a_band(client, rated_churches):
    """There is nothing to compare it against. This is why the UI has to say how
    many parishes a band filter is hiding -- silently dropping five in six looks
    like an answer."""
    for band in ("high", "low", "catholic", "evangelical"):
        body = client.get("/api/churches",
                          params={"state": "CO", "churchmanship": band}).json()
        assert "cm3" not in [c["id"] for c in body["results"]], band


def test_an_unknown_band_is_ignored_rather_than_matching_everything(client, rated_churches):
    """A typo must not silently turn the filter off and return the whole state."""
    unfiltered = client.get("/api/churches", params={"state": "CO"}).json()["total"]
    bogus = client.get("/api/churches",
                       params={"state": "CO", "churchmanship": "sideways"}).json()
    assert bogus["total"] == unfiltered
