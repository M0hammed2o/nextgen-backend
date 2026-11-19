"""Unit tests (no database) for the local email mocks and verification token helpers."""
import hashlib
import logging

import pytest

from backend.app.email import sender as mail
from backend.app.services import email_verification as ev


@pytest.fixture(autouse=True)
def _reset_sender():
    yield
    mail.set_email_sender(None)


def test_tokens_are_long_random_and_only_their_sha256_is_kept():
    a, b = ev.new_raw_token(), ev.new_raw_token()
    assert a != b and len(a) >= 43
    assert ev.hash_token(a) == hashlib.sha256(a.encode()).hexdigest() and a not in ev.hash_token(a)


def test_link_carries_the_token_in_the_fragment_not_the_query():
    link = ev.verification_link("RAW-TOKEN")
    assert link.endswith("/verify-email#token=RAW-TOKEN") and "?" not in link


def test_constants_match_the_owner_requirements():
    assert ev.TOKEN_TTL.total_seconds() == 24 * 3600
    assert ev.RESEND_COOLDOWN.total_seconds() == 60 and ev.RESEND_HOURLY_CAP == 5
    assert ev.ABANDON_AFTER.days == 7


async def test_console_sender_never_logs_the_body_or_the_local_part(caplog):
    caplog.set_level(logging.INFO, logger="nextgen.email")
    msg = ev.verification_message("vendor.person@example.com", "SECRET-TOKEN-123", "K7Q2XM")
    await mail.ConsoleEmailSender().send(msg)
    text = caplog.text
    assert "SECRET-TOKEN-123" not in text and "vendor.person" not in text and "K7Q2XM" not in text
    assert "example.com" in text


async def test_send_safely_swallows_failures():
    class Boom:
        async def send(self, message):
            raise RuntimeError("provider down")

    mail.set_email_sender(Boom())
    assert await mail.send_safely(mail.EmailMessage(to="a@b.c", subject="s", text="t")) is False


def test_file_backend_is_refused_outside_development_and_test(monkeypatch):
    settings = mail.get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "file")
    monkeypatch.setattr(settings, "EMAIL_FILE_DIR", "unused")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        mail._build()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp-not-wired")
    assert isinstance(mail._build(), mail.ConsoleEmailSender)  # unknown backend never pretends to deliver


def test_releasable_only_for_an_abandoned_never_verified_self_registration():
    from datetime import datetime, timezone

    from shared.models.user import BusinessUser

    now = datetime.now(timezone.utc)
    mk = lambda **kw: BusinessUser(role=kw.pop("role", "OWNER"), **kw)  # noqa: E731
    assert ev.releasable_abandoned_owner(mk(email_verification_required=True, registration_abandoned_at=now))
    assert not ev.releasable_abandoned_owner(mk(email_verification_required=True))  # still inside 7 days
    assert not ev.releasable_abandoned_owner(mk(email_verification_required=True, email_verified_at=now,
                                                registration_abandoned_at=now))
    assert not ev.releasable_abandoned_owner(mk(email_verification_required=False, registration_abandoned_at=now))
    assert not ev.releasable_abandoned_owner(mk(role="MANAGER", email_verification_required=True,
                                                registration_abandoned_at=now))
