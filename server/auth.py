"""Passwords, sessions and CSRF.

Design notes, since these are the decisions that matter:

* Passwords go through argon2id. The library picks and encodes its own salt and
  parameters, and `needs_rehash` lets us raise the cost later without forcing a
  reset.
* The session cookie holds 256 bits of `secrets` randomness. The database holds
  only its SHA-256. Reading the sessions table therefore does not let anyone mint
  a cookie -- there is no reversible secret in there.
* CSRF is double-submit: a second, non-HttpOnly cookie the page reads and echoes
  in a header. An attacker's site can make the browser send our cookies but
  cannot read them to build the header, and cannot set the header cross-origin
  without CORS permission we never grant.
* Login is throttled per email and per address, and always answers the same way
  whether or not the email exists.
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

SESSION_COOKIE = "cf_session"
CSRF_COOKIE = "cf_csrf"
CSRF_HEADER = "x-csrf-token"

SESSION_DAYS = 30
SESSION_IDLE_DAYS = 14

# Throttling: attempts allowed inside the window, per bucket.
LOGIN_MAX_PER_EMAIL = 8
LOGIN_MAX_PER_IP = 30
REGISTER_MAX_PER_IP = 5
THROTTLE_WINDOW_MINUTES = 15

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256          # argon2 is happy with long input; this stops abuse
MAX_EMAIL_LENGTH = 254             # RFC 5321

# Deliberately short. A real deployment should check against a leaked-password
# corpus (k-anonymity against Pwned Passwords, or a local bloom filter); this
# list only catches the most obvious attempts so the rule is visible in tests.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwertyuiop", "letmein123", "iloveyou1", "admin12345", "welcome123",
    "changeme123", "churchfind", "passw0rd123", "abc123456", "football123",
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

_hasher = PasswordHasher()


# ------------------------------------------------------------------- utilities

def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def token_hash(raw):
    """SHA-256 is right here: the input is already 256 bits of entropy, so there
    is nothing for an attacker to guess and no reason to pay for a slow KDF."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------- validation

class ValidationError(ValueError):
    """Carries a message safe to show the user."""


def normalise_email(raw):
    email = (raw or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(email):
        raise ValidationError("Enter a valid email address.")
    return email


def validate_password(password, email=None):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(f"Keep it under {MAX_PASSWORD_LENGTH} characters.")
    if password.lower() in COMMON_PASSWORDS:
        raise ValidationError("That password is too common. Pick something else.")
    # All digits is a PIN however long it is: a 12-digit numeric password has
    # about 40 bits of entropy against a 10-character mixed one's 65, and people
    # overwhelmingly pick dates and phone numbers.
    if password.isdigit():
        raise ValidationError("Use more than digits.")
    if len(set(password)) < 5:
        raise ValidationError("That password repeats too few characters.")
    if email and password.lower() == email.split("@")[0].lower():
        raise ValidationError("Your password cannot be your email address.")
    return password


def hash_password(password):
    return _hasher.hash(password)


def verify_password(stored_hash, password):
    """False on any failure -- a malformed hash in the row is a mismatch, not a crash."""
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash):
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------- throttle

def record_attempt(connection, bucket, success):
    connection.execute(
        "INSERT INTO auth_attempts (bucket, at, success) VALUES (?, ?, ?)",
        (bucket, iso(now()), 1 if success else 0),
    )


def attempts_since(connection, bucket, minutes=THROTTLE_WINDOW_MINUTES):
    cutoff = iso(now() - timedelta(minutes=minutes))
    row = connection.execute(
        "SELECT COUNT(*) FROM auth_attempts WHERE bucket = ? AND at >= ? AND success = 0",
        (bucket, cutoff),
    ).fetchone()
    return row[0]


def clear_attempts(connection, bucket):
    connection.execute("DELETE FROM auth_attempts WHERE bucket = ?", (bucket,))


def prune_attempts(connection):
    cutoff = iso(now() - timedelta(hours=24))
    connection.execute("DELETE FROM auth_attempts WHERE at < ?", (cutoff,))


# --------------------------------------------------------------------- sessions

def create_session(connection, user_id, user_agent=""):
    """Mint a session. Returns the raw token and CSRF token for the cookies --
    the caller must not persist either; only their hashes go to the database."""
    raw_token = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(32)
    moment = now()

    connection.execute(
        """INSERT INTO sessions
           (token_hash, user_id, csrf_hash, created_at, expires_at, last_seen_at, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            token_hash(raw_token), user_id, token_hash(raw_csrf),
            iso(moment), iso(moment + timedelta(days=SESSION_DAYS)),
            iso(moment), (user_agent or "")[:300],
        ),
    )
    return raw_token, raw_csrf


def load_session(connection, raw_token):
    """Return the session row for a cookie token, or None.

    Expired and idle sessions are deleted on the way past, so a stale cookie
    cannot be resurrected by clock games elsewhere.
    """
    if not raw_token:
        return None

    row = connection.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (token_hash(raw_token),)
    ).fetchone()
    if row is None:
        return None

    moment = now()
    if parse_iso(row["expires_at"]) <= moment:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        connection.commit()
        return None
    if parse_iso(row["last_seen_at"]) + timedelta(days=SESSION_IDLE_DAYS) <= moment:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        connection.commit()
        return None

    return row


def touch_session(connection, token_hash_value):
    connection.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
        (iso(now()), token_hash_value),
    )


def destroy_session(connection, raw_token):
    if raw_token:
        connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(raw_token),))


def destroy_user_sessions(connection, user_id):
    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def prune_sessions(connection):
    connection.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(now()),))


def csrf_matches(session_row, header_value):
    """Constant-time compare so the check cannot be timed character by character."""
    if not header_value or session_row is None:
        return False
    return hmac.compare_digest(session_row["csrf_hash"], token_hash(header_value))


# ---------------------------------------------------------------------- cookies

def secure_cookies():
    """Secure cookies require HTTPS, which localhost usually is not.

    Defaults to on. Set CHURCHFIND_INSECURE_COOKIES=1 for local http development
    only -- the name is deliberately unpleasant so it does not end up in a
    deployment by accident.
    """
    return os.environ.get("CHURCHFIND_INSECURE_COOKIES", "") != "1"


# ------------------------------------------------------------- password resets

# An hour. Long enough to walk to a different device and find the mail; short
# enough that a link left sitting in an inbox stops being a spare key.
RESET_TTL_MINUTES = 60
RESET_MAX_PER_EMAIL = 3
RESET_MAX_PER_IP = 10


def create_reset(connection, user_id, requested_ip=""):
    """Mint a reset token. Returns the raw token; only its hash is stored."""
    raw = secrets.token_urlsafe(32)
    moment = now()
    connection.execute(
        "INSERT INTO password_resets "
        "(token_hash, user_id, created_at, expires_at, requested_ip) VALUES (?,?,?,?,?)",
        (token_hash(raw), user_id, iso(moment),
         iso(moment + timedelta(minutes=RESET_TTL_MINUTES)), requested_ip),
    )
    return raw


def load_reset(connection, raw_token):
    """The row for a token, or None if it is unknown, used or expired.

    All three failures collapse to None on purpose. Telling the difference would
    let somebody probe which tokens have existed.
    """
    if not raw_token:
        return None
    row = connection.execute(
        "SELECT token_hash, user_id, expires_at, used_at FROM password_resets "
        "WHERE token_hash = ?", (token_hash(raw_token),)
    ).fetchone()
    if row is None or row["used_at"]:
        return None
    if parse_iso(row["expires_at"]) <= now():
        return None
    return row


def consume_reset(connection, row, new_password_hash):
    """Set the password, burn the token, and end every session.

    Ending sessions matters: the commonest reason to reset a password is that
    somebody else might have it. Leaving their session alive would make the reset
    a gesture rather than a remedy.

    Every other outstanding reset for the user is burned too, so an attacker who
    also requested one cannot use it afterwards.
    """
    moment = iso(now())
    connection.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (new_password_hash, row["user_id"]),
    )
    connection.execute(
        "UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at = ''",
        (moment, row["user_id"]),
    )
    connection.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))


def prune_resets(connection):
    connection.execute("DELETE FROM password_resets WHERE expires_at <= ?", (iso(now()),))
