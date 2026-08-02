"""ChurchFind API and static host.

Run it:
    uvicorn server.app:app --reload            # from the repository root
    CHURCHFIND_INSECURE_COOKIES=1 ...          # only for local http

The site works without this server -- it falls back to the static JSON files in
data/. What the server adds is accounts, saved churches and server-side search
over the whole country instead of per-state downloads.
"""

import os
import sys
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import auth  # noqa: E402
import queries  # noqa: E402
from db import connect, init_schema  # noqa: E402
from normalize import FAMILY_LABELS  # noqa: E402
from states import STATE_NAMES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="ChurchFind", version="1.0", docs_url="/api/docs", redoc_url=None)

_connection = None
_facets_cache = None


# ------------------------------------------------------------------ plumbing

@app.on_event("startup")
def startup():
    global _connection
    _connection = connect()
    init_schema(_connection)
    auth.prune_sessions(_connection)
    auth.prune_attempts(_connection)
    _connection.commit()


@app.on_event("shutdown")
def shutdown():
    if _connection is not None:
        _connection.close()


def db():
    if _connection is None:
        raise HTTPException(503, "Database is not ready.")
    return _connection


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # The page loads nothing from another origin except OSM tiles and the
    # geocoder, so the policy can be tight. 'unsafe-inline' is absent: all
    # scripts and styles are files.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self' https://nominatim.openstreetmap.org; "
        "script-src 'self'; style-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(self), interest-cohort=()"
    return response


def client_ip(request: Request):
    return request.client.host if request.client else "unknown"


def set_session_cookies(response: Response, raw_token, raw_csrf):
    secure = auth.secure_cookies()
    max_age = auth.SESSION_DAYS * 24 * 3600
    response.set_cookie(
        auth.SESSION_COOKIE, raw_token, max_age=max_age, httponly=True,
        secure=secure, samesite="lax", path="/",
    )
    # Readable by the page on purpose -- it has to echo this back in a header.
    response.set_cookie(
        auth.CSRF_COOKIE, raw_csrf, max_age=max_age, httponly=False,
        secure=secure, samesite="lax", path="/",
    )


def clear_session_cookies(response: Response):
    for name in (auth.SESSION_COOKIE, auth.CSRF_COOKIE):
        response.delete_cookie(name, path="/")


# ------------------------------------------------------------ auth dependencies

def current_session(cf_session: str = Cookie(default=None)):
    if not cf_session:
        return None
    session = auth.load_session(db(), cf_session)
    if session is not None:
        auth.touch_session(db(), session["token_hash"])
        db().commit()
    return session


def current_user(session=Depends(current_session)):
    if session is None:
        return None
    row = db().execute(
        "SELECT id, email, display_name, created_at, home_lat, home_lon, home_label, is_active "
        "FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    if row is None or not row["is_active"]:
        return None
    return dict(row)


def require_user(user=Depends(current_user)):
    if user is None:
        raise HTTPException(401, "Sign in to do that.")
    return user


def require_csrf(session=Depends(current_session), x_csrf_token: str = Header(default=None)):
    """Every state-changing request on an authenticated session carries this."""
    if session is None:
        raise HTTPException(401, "Sign in to do that.")
    if not auth.csrf_matches(session, x_csrf_token):
        raise HTTPException(403, "Your session token did not match. Reload and try again.")
    return session


def public_user(user):
    return {
        "email": user["email"],
        "displayName": user["display_name"],
        "createdAt": user["created_at"],
        "home": (
            {"lat": user["home_lat"], "lon": user["home_lon"], "label": user["home_label"]}
            if user["home_lat"] is not None else None
        ),
    }


# ------------------------------------------------------------------- schemas

class Credentials(BaseModel):
    email: str = Field(max_length=auth.MAX_EMAIL_LENGTH)
    password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    display_name: str = Field(default="", max_length=80)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=auth.MAX_PASSWORD_LENGTH)


class SavePayload(BaseModel):
    church_id: str = Field(max_length=40)
    note: str = Field(default="", max_length=500)


class HomePayload(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str = Field(default="", max_length=200)


class ReportPayload(BaseModel):
    church_id: str = Field(max_length=40)
    field: str = Field(max_length=40)
    suggestion: str = Field(max_length=1000)


# --------------------------------------------------------------- church routes

@app.get("/api/health")
def health():
    meta = queries.dataset_meta(db())
    return {
        "ok": True,
        "churches": int(meta.get("church_count", 0) or 0),
        "accounts": True,
        "snapshot": meta.get("osm_snapshot", ""),
    }


@app.get("/api/meta")
def meta():
    global _facets_cache
    if _facets_cache is None:
        _facets_cache = queries.facets(db(), FAMILY_LABELS, STATE_NAMES)
        _facets_cache["dataset"] = queries.dataset_meta(db())
    return _facets_cache


@app.get("/api/churches")
def search_churches(
    lat: float = None,
    lon: float = None,
    radius: float = 25,
    state: str = None,
    q: str = None,
    family: str = None,
    denomination: str = None,
    has_website: bool = False,
    has_phone: bool = False,
    has_services: bool = False,
    wheelchair: bool = False,
    sort: str = "distance",
    limit: int = 50,
    offset: int = 0,
):
    if (lat is None) != (lon is None):
        raise HTTPException(422, "Provide both lat and lon, or neither.")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "Coordinates are out of range.")
    if state is not None and state.upper() not in STATE_NAMES:
        raise HTTPException(422, "Unknown state code.")

    return queries.search(db(), {
        "lat": lat, "lon": lon, "radius": radius,
        "state": state.upper() if state else None,
        "q": q,
        # Repeated query params arrive comma-joined from the UI; both work.
        "families": [f for f in (family or "").split(",") if f],
        "denominations": [d for d in (denomination or "").split(",") if d],
        "has_website": has_website, "has_phone": has_phone,
        "has_services": has_services, "wheelchair": wheelchair,
        "sort": sort, "limit": limit, "offset": offset,
    })


@app.get("/api/churches/{church_id}")
def church_detail(church_id: str, user=Depends(current_user)):
    church = queries.get_church(db(), church_id)
    if church is None:
        raise HTTPException(404, "No church with that id.")
    if user:
        row = db().execute(
            "SELECT note FROM saved_churches WHERE user_id = ? AND church_id = ?",
            (user["id"], church_id),
        ).fetchone()
        church["saved"] = row is not None
        church["note"] = row["note"] if row else ""
    return church


# ----------------------------------------------------------------- auth routes

@app.post("/api/auth/register")
def register(payload: Credentials, request: Request, response: Response):
    connection = db()
    ip_bucket = f"register:ip:{client_ip(request)}"

    if auth.attempts_since(connection, ip_bucket) >= auth.REGISTER_MAX_PER_IP:
        raise HTTPException(429, "Too many sign-ups from here. Try again later.")

    try:
        email = auth.normalise_email(payload.email)
        auth.validate_password(payload.password, email)
    except auth.ValidationError as error:
        auth.record_attempt(connection, ip_bucket, False)
        connection.commit()
        raise HTTPException(422, str(error))

    existing = connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        auth.record_attempt(connection, ip_bucket, False)
        connection.commit()
        # Registration cannot hide that an address is taken -- the account has to
        # be unique. Login is where enumeration actually matters, and that one
        # gives nothing away.
        raise HTTPException(409, "An account already exists for that email.")

    cursor = connection.execute(
        "INSERT INTO users (email, password_hash, display_name, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (email, auth.hash_password(payload.password), payload.display_name.strip()[:80],
         auth.iso(auth.now()), auth.iso(auth.now())),
    )
    user_id = cursor.lastrowid

    raw_token, raw_csrf = auth.create_session(
        connection, user_id, request.headers.get("user-agent", "")
    )
    auth.record_attempt(connection, ip_bucket, True)
    connection.commit()

    set_session_cookies(response, raw_token, raw_csrf)
    user = connection.execute(
        "SELECT id, email, display_name, created_at, home_lat, home_lon, home_label, is_active "
        "FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return {"user": public_user(dict(user))}


@app.post("/api/auth/login")
def login(payload: Credentials, request: Request, response: Response):
    connection = db()
    ip_bucket = f"login:ip:{client_ip(request)}"

    if auth.attempts_since(connection, ip_bucket) >= auth.LOGIN_MAX_PER_IP:
        raise HTTPException(429, "Too many attempts from here. Wait a few minutes.")

    try:
        email = auth.normalise_email(payload.email)
    except auth.ValidationError:
        auth.record_attempt(connection, ip_bucket, False)
        connection.commit()
        raise HTTPException(401, "Email or password is incorrect.")

    email_bucket = f"login:{email}"
    if auth.attempts_since(connection, email_bucket) >= auth.LOGIN_MAX_PER_EMAIL:
        raise HTTPException(429, "Too many attempts for that account. Wait a few minutes.")

    row = connection.execute(
        "SELECT id, password_hash, is_active FROM users WHERE email = ?", (email,)
    ).fetchone()

    # Hash a throwaway password when the account is missing, so a request for an
    # unknown address costs the same time as one for a known address.
    stored = row["password_hash"] if row else auth.hash_password("no-such-account-placeholder")
    ok = auth.verify_password(stored, payload.password) and row is not None and row["is_active"]

    if not ok:
        auth.record_attempt(connection, email_bucket, False)
        auth.record_attempt(connection, ip_bucket, False)
        connection.commit()
        raise HTTPException(401, "Email or password is incorrect.")

    if auth.needs_rehash(stored):
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (auth.hash_password(payload.password), row["id"]),
        )

    connection.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?", (auth.iso(auth.now()), row["id"])
    )
    auth.clear_attempts(connection, email_bucket)

    raw_token, raw_csrf = auth.create_session(
        connection, row["id"], request.headers.get("user-agent", "")
    )
    connection.commit()

    set_session_cookies(response, raw_token, raw_csrf)
    user = connection.execute(
        "SELECT id, email, display_name, created_at, home_lat, home_lon, home_label, is_active "
        "FROM users WHERE id = ?", (row["id"],)
    ).fetchone()
    return {"user": public_user(dict(user))}


@app.post("/api/auth/logout")
def logout(response: Response, cf_session: str = Cookie(default=None), _=Depends(require_csrf)):
    auth.destroy_session(db(), cf_session)
    db().commit()
    clear_session_cookies(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"user": public_user(user) if user else None}


@app.post("/api/auth/password")
def change_password(payload: PasswordChange, user=Depends(require_user), _=Depends(require_csrf)):
    connection = db()
    row = connection.execute(
        "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
    ).fetchone()

    if not auth.verify_password(row["password_hash"], payload.current_password):
        raise HTTPException(403, "Your current password is incorrect.")
    try:
        auth.validate_password(payload.new_password, user["email"])
    except auth.ValidationError as error:
        raise HTTPException(422, str(error))

    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(payload.new_password), user["id"]),
    )
    # Changing a password signs every other device out, which is the whole point
    # of changing it after a scare.
    auth.destroy_user_sessions(connection, user["id"])
    connection.commit()
    return {"ok": True, "signedOutEverywhere": True}


# ---------------------------------------------------------------- account data

@app.get("/api/saved")
def list_saved(user=Depends(require_user)):
    rows = db().execute(
        "SELECT church_id, note, created_at FROM saved_churches "
        "WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    notes = {row["church_id"]: row["note"] for row in rows}
    order = {row["church_id"]: i for i, row in enumerate(rows)}

    churches = queries.get_churches(db(), list(notes))
    for church in churches:
        church["note"] = notes.get(church["id"], "")
        church["saved"] = True
    churches.sort(key=lambda church: order.get(church["id"], 0))
    return {"total": len(churches), "results": churches}


@app.post("/api/saved")
def save_church(payload: SavePayload, user=Depends(require_user), _=Depends(require_csrf)):
    if queries.get_church(db(), payload.church_id) is None:
        raise HTTPException(404, "No church with that id.")
    db().execute(
        "INSERT INTO saved_churches (user_id, church_id, note, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, church_id) DO UPDATE SET note = excluded.note",
        (user["id"], payload.church_id, payload.note.strip(), auth.iso(auth.now())),
    )
    db().commit()
    return {"ok": True, "saved": True}


@app.delete("/api/saved/{church_id}")
def unsave_church(church_id: str, user=Depends(require_user), _=Depends(require_csrf)):
    db().execute(
        "DELETE FROM saved_churches WHERE user_id = ? AND church_id = ?",
        (user["id"], church_id),
    )
    db().commit()
    return {"ok": True, "saved": False}


@app.put("/api/me/home")
def set_home(payload: HomePayload, user=Depends(require_user), _=Depends(require_csrf)):
    db().execute(
        "UPDATE users SET home_lat = ?, home_lon = ?, home_label = ? WHERE id = ?",
        (payload.lat, payload.lon, payload.label.strip()[:200], user["id"]),
    )
    db().commit()
    return {"ok": True}


@app.post("/api/reports")
def report_correction(payload: ReportPayload, user=Depends(require_user), _=Depends(require_csrf)):
    if queries.get_church(db(), payload.church_id) is None:
        raise HTTPException(404, "No church with that id.")
    suggestion = payload.suggestion.strip()
    if not suggestion:
        raise HTTPException(422, "Say what should change.")

    db().execute(
        "INSERT INTO correction_reports (user_id, church_id, field, suggestion, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user["id"], payload.church_id, payload.field.strip()[:40], suggestion[:1000],
         auth.iso(auth.now())),
    )
    db().commit()
    # Reports are a queue for a human. The fix belongs upstream in OSM, so the
    # response says where to make it stick.
    return {
        "ok": True,
        "note": "Thanks. Corrections also need fixing in OpenStreetMap to survive "
                "the next scrape: https://www.openstreetmap.org/fixthemap",
    }


# ------------------------------------------------------------------- the site

@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return FileResponse(ROOT / "index.html")


for mount in ("assets", "data"):
    directory = ROOT / mount
    if directory.exists():
        app.mount(f"/{mount}", StaticFiles(directory=directory), name=mount)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
