"""
backend/app/bot/meta_errors.py — parsing/classification of Meta WhatsApp
Cloud API error responses (Wave 0A P1).

Canned Meta error bodies for the codes this pipeline is required to treat
specially: 190 (invalid token — platform-wide), 131047 (outside 24h
service window — retrying is pointless), 130429/131056 (rate limits —
back off), 131026 (invalid recipient).
"""
from backend.app.bot.meta_errors import (
    INVALID_RECIPIENT,
    OUTSIDE_SERVICE_WINDOW,
    TOKEN_INVALID,
    is_outside_service_window,
    is_rate_limited,
    log_meta_error,
    parse_meta_error,
    transport_error,
)


def _body(code: int, message: str, subcode: int | None = None, details: str | None = None) -> str:
    import json
    error = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    if details:
        error["error_data"] = {"details": details}
    return json.dumps({"error": error})


# ── Code 190: invalid/expired token — permanent, platform-wide ─────────────

def test_code_190_is_not_retryable():
    info = parse_meta_error(401, _body(190, "Error validating access token"))
    assert info.code == TOKEN_INVALID
    assert info.retryable is False
    assert "META_190" in info.last_error


def test_code_190_never_leaks_the_token_into_last_error():
    info = parse_meta_error(401, _body(190, "Error validating access token: Session has expired"))
    assert "Bearer" not in info.last_error
    assert "WHATSAPP_DEFAULT_ACCESS_TOKEN" not in info.last_error


# ── Code 131047: outside 24h service window — permanent, don't retry ───────

def test_code_131047_outside_service_window_is_not_retryable():
    info = parse_meta_error(
        470, _body(131047, "Message failed to send because more than 24 hours have passed"),
    )
    assert info.code == OUTSIDE_SERVICE_WINDOW
    assert info.retryable is False
    assert is_outside_service_window(info) is True


def test_is_outside_service_window_false_for_other_codes():
    info = parse_meta_error(401, _body(190, "Error validating access token"))
    assert is_outside_service_window(info) is False
    assert is_outside_service_window(None) is False


# ── Rate limits: retryable, back off ────────────────────────────────────────

def test_rate_limit_code_130429_is_retryable():
    info = parse_meta_error(429, _body(130429, "Too many requests"))
    assert info.retryable is True
    assert is_rate_limited(info) is True


def test_rate_limit_code_131056_is_retryable():
    info = parse_meta_error(429, _body(131056, "Pair rate limit hit"))
    assert info.retryable is True
    assert is_rate_limited(info) is True


# ── Invalid recipient: permanent ────────────────────────────────────────────

def test_invalid_recipient_131026_is_not_retryable():
    info = parse_meta_error(400, _body(131026, "Message undeliverable"))
    assert info.code == INVALID_RECIPIENT
    assert info.retryable is False


# ── error_data.details preserved, unknown codes default retryable ──────────

def test_error_data_details_included_in_last_error():
    info = parse_meta_error(400, _body(100, "Invalid parameter", details="template name is invalid"))
    assert "template name is invalid" in info.last_error


def test_unknown_code_defaults_to_retryable():
    """An unseen Meta error code must default to retryable — treating an
    unrecognised code as permanent risks silently dropping a real
    message on a transient issue this pipeline hasn't seen before."""
    info = parse_meta_error(500, _body(99999, "Some new Meta error"))
    assert info.retryable is True


# ── Malformed / non-JSON bodies never raise ─────────────────────────────────

def test_non_json_body_does_not_raise():
    info = parse_meta_error(500, "<html>Internal Server Error</html>")
    assert info.code is None
    assert info.retryable is True
    assert info.last_error  # still produces something usable


def test_empty_body_does_not_raise():
    info = parse_meta_error(503, "")
    assert info.retryable is True


# ── Transport errors (timeouts, exceptions) are always retryable ───────────

def test_transport_error_is_retryable():
    info = transport_error("Meta API timeout after 10s")
    assert info.retryable is True
    assert "TRANSPORT_ERROR" in info.last_error


# ── Logging: code 190 gets the stable greppable prefix ─────────────────────

def test_log_meta_error_uses_greppable_prefix_for_token_invalid(caplog):
    import logging
    info = parse_meta_error(401, _body(190, "Error validating access token"))
    with caplog.at_level(logging.ERROR, logger="nextgen.bot.meta_errors"):
        log_meta_error(info, "1234567890")
    assert any("META_TOKEN_INVALID" in rec.message for rec in caplog.records)


def test_log_meta_error_does_not_use_token_invalid_prefix_for_other_codes(caplog):
    import logging
    info = parse_meta_error(470, _body(131047, "outside window"))
    with caplog.at_level(logging.ERROR, logger="nextgen.bot.meta_errors"):
        log_meta_error(info, "1234567890")
    assert not any("META_TOKEN_INVALID" in rec.message for rec in caplog.records)


# ── Merge of meta_errors with the customer-owned connection send path ───────

class _FakeGraphError(Exception):
    def __init__(self, message, status_code=None, error_code=None, error_subcode=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_subcode = error_subcode


def test_from_graph_error_classifies_invalid_token_as_permanent():
    from backend.app.bot.meta_errors import from_graph_error

    info = from_graph_error(_FakeGraphError("token invalid", 401, 190))
    assert info.code == 190
    assert info.retryable is False
    assert info.last_error.startswith("META_190: ")


def test_from_graph_error_keeps_rate_limits_retryable():
    from backend.app.bot.meta_errors import from_graph_error

    info = from_graph_error(_FakeGraphError("slow down", 429, 130429))
    assert info.retryable is True
    assert info.last_error.startswith("META_130429: ")


def test_from_graph_error_outside_window_is_detected():
    from backend.app.bot.meta_errors import from_graph_error, is_outside_service_window

    assert is_outside_service_window(from_graph_error(_FakeGraphError("window", 400, 131047)))


def test_not_sendable_is_permanent_so_outbox_does_not_burn_retries():
    from backend.app.bot.meta_errors import not_sendable

    info = not_sendable("WhatsApp connection is NEEDS_ATTENTION — reconnect required")
    assert info.retryable is False
    assert info.last_error.startswith("NOT_SENDABLE: ")


async def test_outbox_fails_immediately_when_connection_needs_reconnecting():
    import types
    import uuid
    from unittest.mock import AsyncMock, patch

    from backend.app.bot.meta_errors import not_sendable
    from backend.app.bot.outbox_worker import _process_single

    business = types.SimpleNamespace(
        id=uuid.uuid4(), whatsapp_phone_number_id="123", is_whatsapp_enabled=True
    )

    class _Result:
        def scalar_one_or_none(self):
            return business

    db = types.SimpleNamespace(
        execute=AsyncMock(return_value=_Result()), flush=AsyncMock()
    )
    msg = types.SimpleNamespace(
        business_id=business.id, customer_wa_id="27820000000", message_text="hi",
        attempts=0, max_attempts=3, status="PENDING", last_error=None,
        sent_wa_message_id=None, sent_at=None, scheduled_at=None,
    )
    with patch(
        "backend.app.bot.outbox_worker._send_via_business_connection",
        AsyncMock(return_value=(None, not_sendable("connection NEEDS_ATTENTION"))),
    ):
        await _process_single(db, msg)

    assert msg.status == "FAILED"
    assert msg.attempts == 1
    assert msg.last_error.startswith("NOT_SENDABLE: ")
