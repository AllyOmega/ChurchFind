"""Tests for mail configuration.

No mail is sent here. What is checked is the resolution of settings and the
promise that an unconfigured deployment degrades instead of failing -- both of
which have been wrong before.

    python -m pytest server/test_mailer.py -q
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mailer  # noqa: E402

SMTP_VARS = [
    "CHURCHFIND_SMTP_HOST", "CHURCHFIND_SMTP_PORT", "CHURCHFIND_SMTP_USER",
    "CHURCHFIND_SMTP_PASSWORD", "CHURCHFIND_SMTP_TLS", "CHURCHFIND_RESEND_API_KEY",
    "CHURCHFIND_MAIL_FROM", "CHURCHFIND_BASE_URL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SMTP_VARS:
        monkeypatch.delenv(name, raising=False)


def test_nothing_configured_means_nothing_configured():
    assert mailer.configured() is False
    assert mailer.settings()["host"] == ""


def test_a_resend_key_alone_is_enough(monkeypatch):
    """One secret rather than four. Resend's host, port, username and TLS mode
    are always the same, so only the key is worth asking for."""
    monkeypatch.setenv("CHURCHFIND_RESEND_API_KEY", "re_testkey")

    config = mailer.settings()
    assert mailer.configured() is True
    assert config["host"] == "smtp.resend.com"
    assert config["user"] == "resend"
    assert config["password"] == "re_testkey"
    assert config["port"] == 587
    assert config["tls"] == "starttls"


def test_explicit_smtp_settings_beat_the_resend_shortcut(monkeypatch):
    """This is what keeps Resend a convenience rather than a special case:
    pointing at another provider must never mean unpicking it."""
    monkeypatch.setenv("CHURCHFIND_RESEND_API_KEY", "re_testkey")
    monkeypatch.setenv("CHURCHFIND_SMTP_HOST", "smtp.example.net")
    monkeypatch.setenv("CHURCHFIND_SMTP_USER", "someone")
    monkeypatch.setenv("CHURCHFIND_SMTP_PASSWORD", "hunter2")
    monkeypatch.setenv("CHURCHFIND_SMTP_PORT", "465")
    monkeypatch.setenv("CHURCHFIND_SMTP_TLS", "ssl")

    config = mailer.settings()
    assert config["host"] == "smtp.example.net"
    assert config["user"] == "someone"
    assert config["password"] == "hunter2"
    assert config["port"] == 465
    assert config["tls"] == "ssl"


def test_a_blank_resend_key_does_not_count_as_configured(monkeypatch):
    """`fly secrets set X=` and an unset variable should mean the same thing."""
    monkeypatch.setenv("CHURCHFIND_RESEND_API_KEY", "   ")
    assert mailer.configured() is False


def test_unconfigured_send_logs_the_message_and_does_not_raise(caplog):
    """The only way to obtain a reset link without a mail server, so it has to
    reach the terminal. It logged at INFO once, which with no logging configured
    means an effective level of WARNING and a message on the floor -- the
    documented development path was dead until somebody ran it."""
    with caplog.at_level(logging.WARNING, logger="churchfind.mail"):
        sent = mailer.send("someone@example.org", "Subject here", "Body here")

    assert sent is False
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    logged = caplog.text
    assert "someone@example.org" in logged and "Body here" in logged


def test_the_reset_link_uses_the_public_base_url(monkeypatch, caplog):
    """It goes in an email, so localhost is worse than useless."""
    monkeypatch.setenv("CHURCHFIND_BASE_URL", "https://churchfind.example/")
    with caplog.at_level(logging.WARNING, logger="churchfind.mail"):
        mailer.password_reset("someone@example.org", "TOKEN123")

    # Trailing slash stripped, so the join is clean rather than doubled.
    assert "https://churchfind.example/?reset=TOKEN123" in caplog.text


def test_a_send_failure_never_raises(monkeypatch):
    """A mail server being down is not a reason for a form submission to 500."""
    monkeypatch.setenv("CHURCHFIND_RESEND_API_KEY", "re_testkey")

    def explode(*args, **kwargs):
        raise OSError("network is unreachable")

    monkeypatch.setattr(mailer.smtplib, "SMTP", explode)
    assert mailer.send("someone@example.org", "Subject", "Body") is False
