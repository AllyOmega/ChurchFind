"""Sending email over SMTP, and doing nothing loudly when it is not configured.

Nothing here needs a third-party package: `smtplib` and `email.message` are
standard library, and SMTP works with every provider, which is why it is the
default rather than one vendor's REST API.

TWO RULES

**An unconfigured deployment must not crash.** With no SMTP host set, `send`
writes the message to the log and returns False. Local development then works
with no setup at all -- the reset link appears in the terminal -- and a
production deploy that forgot the credentials degrades to "the mail did not go"
rather than a 500 on the password-reset form.

**A failure to send is never reported to the caller as a failure.** That is a
decision made in app.py rather than here, and the reason is enumeration: if a
reset request answers differently for a deliverable address than an undeliverable
one, the form becomes a way to test whether somebody has an account. `send`
returns a boolean for logging and for tests; the route ignores it on purpose.

CONFIGURATION

    CHURCHFIND_SMTP_HOST      required, or nothing is sent
    CHURCHFIND_SMTP_PORT      default 587
    CHURCHFIND_SMTP_USER      optional; no login attempted without it
    CHURCHFIND_SMTP_PASSWORD  optional
    CHURCHFIND_SMTP_TLS       "starttls" (default), "ssl", or "none"
    CHURCHFIND_MAIL_FROM      default "ChurchFind <no-reply@localhost>"
    CHURCHFIND_BASE_URL       default "http://localhost:8000" -- the origin that
                              goes in links, so it must be the public one

RESEND

Resend speaks SMTP, so it needs no code of its own. Setting one variable fills
in the four that are always the same for them:

    CHURCHFIND_RESEND_API_KEY   ->  host smtp.resend.com, port 587,
                                    user "resend", starttls

Any explicit CHURCHFIND_SMTP_* still wins, so this is a shortcut rather than a
special case, and switching to another provider means deleting one variable.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger("churchfind.mail")

DEFAULT_PORT = 587
TIMEOUT = 15


# Resend's published SMTP endpoint. Port 587 is their STARTTLS port; 465 is
# implicit SSL if a network ever forces it.
RESEND_HOST = "smtp.resend.com"
RESEND_USER = "resend"


def settings():
    """Resolve SMTP settings, letting a Resend key stand in for four variables.

    Explicit CHURCHFIND_SMTP_* always wins. That ordering is what keeps this a
    convenience rather than a provider special case -- pointing at a different
    SMTP server never requires unpicking anything here.
    """
    resend_key = os.environ.get("CHURCHFIND_RESEND_API_KEY", "").strip()
    return {
        "host": os.environ.get("CHURCHFIND_SMTP_HOST") or (RESEND_HOST if resend_key else ""),
        "port": int(os.environ.get("CHURCHFIND_SMTP_PORT", DEFAULT_PORT)),
        "user": os.environ.get("CHURCHFIND_SMTP_USER") or (RESEND_USER if resend_key else ""),
        "password": os.environ.get("CHURCHFIND_SMTP_PASSWORD") or resend_key,
        "tls": os.environ.get("CHURCHFIND_SMTP_TLS", "starttls").lower(),
    }


def configured():
    return bool(settings()["host"])


def base_url():
    """Origin for links in email. Trailing slash stripped so joins are clean."""
    return os.environ.get("CHURCHFIND_BASE_URL", "http://localhost:8000").rstrip("/")


def mail_from():
    return os.environ.get("CHURCHFIND_MAIL_FROM", "ChurchFind <no-reply@localhost>")


def send(to_address, subject, body):
    """Send one plain-text message. Returns True only if SMTP accepted it.

    Every failure is caught. A mail server being down is not a reason for a
    request to fail, and the caller must not be able to tell the difference
    anyway -- see the enumeration note above.
    """
    if not configured():
        # WARNING, not INFO, and that is load-bearing. Nothing configures logging
        # here, so the root logger has no handlers and an effective level of
        # WARNING; an INFO call is dropped on the floor. This message is the only
        # way to obtain a reset link without a mail server, so a level that does
        # not reach the terminal makes the documented development path dead --
        # which is exactly what it did until somebody ran it.
        #
        # It also is a warning. Mail that did not go is worth saying out loud.
        logger.warning(
            "SMTP is not configured, so this mail was NOT sent. "
            "Set CHURCHFIND_SMTP_HOST to send it for real.\n"
            "  To:      %s\n  Subject: %s\n%s", to_address, subject, body
        )
        return False

    message = EmailMessage()
    message["From"] = mail_from()
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    config = settings()
    host, port = config["host"], config["port"]
    user, password, mode = config["user"], config["password"], config["tls"]

    try:
        if mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with server:
            if mode == "starttls":
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password or "")
            server.send_message(message)
        return True
    except smtplib.SMTPSenderRefused:
        # Worth its own branch because it is the failure everyone hits first:
        # Resend and most providers refuse a From address on a domain you have
        # not verified, and the generic "could not send" gives no clue why.
        logger.exception(
            "The sender address %r was refused. With Resend and most providers "
            "the From domain has to be verified first -- check "
            "CHURCHFIND_MAIL_FROM.", mail_from()
        )
        return False
    except Exception:                                       # noqa: BLE001
        # Broad on purpose: smtplib raises a dozen different exceptions and a
        # socket can fail in ways none of them cover. None of them should reach
        # a user submitting a form.
        logger.exception("Could not send mail to %s", to_address)
        return False


def password_reset(to_address, raw_token):
    """The one message this application sends."""
    link = f"{base_url()}/?reset={raw_token}"
    subject = "Reset your ChurchFind password"
    body = (
        "Somebody asked to reset the ChurchFind password for this address.\n\n"
        f"{link}\n\n"
        "The link works once and expires in an hour.\n\n"
        "If it was not you, nothing has happened to your account and you can\n"
        "ignore this message. Your password has not changed.\n"
    )
    return send(to_address, subject, body)
