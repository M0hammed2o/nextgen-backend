"""
outbox_worker.py send-failure diagnosability (Wave 0A P1).

Before this fix, message_outbox.last_error was always the generic string
"Send failed, retrying" regardless of WHY Meta rejected the send, and a
permanent failure (invalid token, outside the 24h service window, invalid
recipient) still had to burn through max_attempts worth of exponential
backoff before landing on FAILED.
"""
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.bot.outbox_worker import _process_single
from shared.models.audit import MessageOutbox


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, business):
        self._business = business
        self.flushed = 0

    async def execute(self, stmt):
        return FakeResult(scalar=self._business)

    async def flush(self):
        self.flushed += 1


def make_business(phone_number_id="PNID", is_whatsapp_enabled=True):
    return types.SimpleNamespace(
        id=uuid.uuid4(), whatsapp_phone_number_id=phone_number_id,
        is_whatsapp_enabled=is_whatsapp_enabled,
    )


def make_msg(business_id, attempts=0, max_attempts=3):
    msg = MessageOutbox()
    msg.id = uuid.uuid4()
    msg.business_id = business_id
    msg.customer_wa_id = "27821234567"
    msg.message_text = "hello"
    msg.status = "PENDING"
    msg.attempts = attempts
    msg.max_attempts = max_attempts
    msg.last_error = None
    msg.scheduled_at = datetime.now(timezone.utc)
    return msg


def permanent_error(code=190, last_error="META_190: Error validating access token"):
    return MetaErrorInfo(
        status_code=401, code=code, subcode=None, message="bad token",
        details=None, retryable=False, last_error=last_error,
    )


def retryable_error(code=130429, last_error="META_130429: Too many requests"):
    return MetaErrorInfo(
        status_code=429, code=code, subcode=None, message="rate limited",
        details=None, retryable=True, last_error=last_error,
    )


# ── Permanent failure: FAILED immediately, real reason stored ──────────────

async def test_permanent_failure_marks_failed_immediately_not_after_max_attempts():
    business = make_business()
    msg = make_msg(business.id, attempts=0, max_attempts=3)
    db = FakeDB(business)

    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        return_value=(None, permanent_error()),
    ):
        await _process_single(db, msg)

    assert msg.status == "FAILED"
    assert msg.attempts == 1  # did NOT burn through all 3 attempts
    assert "META_190" in msg.last_error


async def test_outside_service_window_is_also_permanent():
    business = make_business()
    msg = make_msg(business.id)
    db = FakeDB(business)

    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        return_value=(None, permanent_error(code=131047, last_error="META_131047: outside window")),
    ):
        await _process_single(db, msg)

    assert msg.status == "FAILED"
    assert "131047" in msg.last_error


# ── Retryable failure: stays PENDING with the real reason, unchanged backoff ─

async def test_retryable_failure_stays_pending_with_specific_reason():
    business = make_business()
    msg = make_msg(business.id, attempts=0, max_attempts=3)
    db = FakeDB(business)

    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        return_value=(None, retryable_error()),
    ):
        await _process_single(db, msg)

    assert msg.status == "PENDING"
    assert "130429" in msg.last_error
    assert msg.last_error != "Send failed, retrying"  # no longer generic


async def test_retryable_failure_at_max_attempts_fails_with_last_reason():
    business = make_business()
    msg = make_msg(business.id, attempts=2, max_attempts=3)  # this attempt -> 3rd
    db = FakeDB(business)

    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        return_value=(None, retryable_error()),
    ):
        await _process_single(db, msg)

    assert msg.status == "FAILED"
    assert "130429" in msg.last_error
    assert "Max attempts" in msg.last_error


# ── Success path is unaffected ──────────────────────────────────────────────

async def test_success_marks_sent():
    business = make_business()
    msg = make_msg(business.id)
    db = FakeDB(business)

    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        return_value=("wamid.xyz", None),
    ):
        await _process_single(db, msg)

    assert msg.status == "SENT"
    assert msg.sent_wa_message_id == "wamid.xyz"


# ── Pre-existing gates unaffected ───────────────────────────────────────────

async def test_missing_business_still_fails_with_original_message():
    msg = make_msg(uuid.uuid4())
    db = FakeDB(None)
    await _process_single(db, msg)
    assert msg.status == "FAILED"
    assert msg.last_error == "Business not found or missing phone_number_id"


async def test_whatsapp_disabled_still_fails_with_original_message():
    business = make_business(is_whatsapp_enabled=False)
    msg = make_msg(business.id)
    db = FakeDB(business)
    await _process_single(db, msg)
    assert msg.status == "FAILED"
    assert msg.last_error == "WhatsApp disabled for this business"
