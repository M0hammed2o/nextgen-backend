"""
Meta WhatsApp Cloud API error parsing/classification.

Deliberately isolated from whatsapp_sender.py / outbox_worker.py: this
module never touches an access token, a URL, or `settings` — it only ever
turns an HTTP status code + response body into a structured, actionable
result. That keeps it safe to import from anywhere in the send path
without any risk of colliding with token-resolution changes happening
elsewhere in the bot pipeline.

Before this module existed, a failed send logged `resp.text[:500]` and
returned None — nothing distinguished an invalid platform token (code 190,
a platform-wide outage worth paging on) from a customer simply being
outside the 24-hour service window (131047, where retrying is pointless)
from a rate limit (130429/131056, where backing off is correct) from an
invalid recipient (131026). Every one of those got the same generic
"Send failed, retrying" and burned all of message_outbox.max_attempts
before giving up, even when retrying could never have worked.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("nextgen.bot.meta_errors")

# ── Known Meta error codes/subcodes this pipeline treats specially ─────────
TOKEN_INVALID = 190
RATE_LIMIT_CODES = frozenset({130429, 131056})
OUTSIDE_SERVICE_WINDOW = 131047
INVALID_RECIPIENT = 131026

# Retrying can never succeed for these — burning message_outbox.max_attempts
# on them just delays the customer/staff finding out. Rate limits are
# deliberately NOT in this set: backing off and retrying IS correct there.
_PERMANENT_CODES = frozenset({TOKEN_INVALID, OUTSIDE_SERVICE_WINDOW, INVALID_RECIPIENT})


@dataclass(frozen=True)
class MetaErrorInfo:
    status_code: int
    code: int | None
    subcode: int | None
    message: str
    details: str | None
    retryable: bool
    last_error: str  # concise, safe to persist in message_outbox.last_error — no tokens, no extra PII


def parse_meta_error(status_code: int, response_text: str) -> MetaErrorInfo:
    """
    Parse Meta's `{"error": {...}}` envelope. Never raises — a malformed or
    non-JSON body still produces a usable (if less specific) result, so a
    parsing bug here can never crash a send attempt.
    """
    code: int | None = None
    subcode: int | None = None
    message = ""
    details: str | None = None

    try:
        body = json.loads(response_text) if response_text else {}
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            subcode = error.get("error_subcode")
            message = error.get("message") or ""
            error_data = error.get("error_data")
            if isinstance(error_data, dict):
                raw_details = error_data.get("details")
                details = str(raw_details)[:200] if raw_details else None
    except (ValueError, TypeError, AttributeError):
        pass

    if not message:
        message = (response_text or "Unknown Meta API error")[:200]

    retryable = code not in _PERMANENT_CODES and subcode not in _PERMANENT_CODES

    reason = message
    if details:
        reason = f"{reason} ({details})"
    prefix = f"META_{code}" if code is not None else f"HTTP_{status_code}"
    if subcode:
        prefix += f".{subcode}"
    last_error = f"{prefix}: {reason}"[:500]

    return MetaErrorInfo(
        status_code=status_code, code=code, subcode=subcode, message=message,
        details=details, retryable=retryable, last_error=last_error,
    )


def from_graph_error(exc) -> MetaErrorInfo:
    """
    Classify a `backend.app.whatsapp.graph_client.GraphAPIError` (the
    customer-owned-connection send path) with the same code table as
    parse_meta_error, so both send paths store and act on failures alike.
    """
    code = getattr(exc, "error_code", None)
    subcode = getattr(exc, "error_subcode", None)
    status_code = getattr(exc, "status_code", None) or 0
    message = (str(exc) or "Unknown Meta API error")[:200]
    retryable = code not in _PERMANENT_CODES and subcode not in _PERMANENT_CODES
    prefix = f"META_{code}" if code is not None else f"HTTP_{status_code}"
    if subcode:
        prefix += f".{subcode}"
    return MetaErrorInfo(
        status_code=status_code, code=code, subcode=subcode, message=message,
        details=None, retryable=retryable, last_error=f"{prefix}: {message}"[:500],
    )


def not_sendable(reason: str) -> MetaErrorInfo:
    """
    NextGen itself cannot attempt the send (no credentials, or the business's
    customer-owned connection needs reconnecting). Retrying within the
    outbox backoff window cannot fix that, so it is permanent.
    """
    return MetaErrorInfo(
        status_code=0, code=None, subcode=None, message=reason, details=None,
        retryable=False, last_error=f"NOT_SENDABLE: {reason}"[:500],
    )


def transport_error(reason: str) -> MetaErrorInfo:
    """
    A non-Meta failure (timeout, connection error, unexpected exception) —
    always retryable, since it says nothing about whether the message
    itself is deliverable.
    """
    return MetaErrorInfo(
        status_code=0, code=None, subcode=None, message=reason, details=None,
        retryable=True, last_error=f"TRANSPORT_ERROR: {reason}"[:500],
    )


def log_meta_error(info: MetaErrorInfo, phone_number_id: str) -> None:
    """
    Single logging call site so every failed send is logged consistently.
    Code 190 (invalid/expired platform token) gets a stable, greppable
    prefix — it means EVERY business on the platform is affected, not just
    this one send, and is worth alerting on distinctly from an ordinary
    per-message failure.
    """
    if info.code == TOKEN_INVALID:
        logger.error(
            "META_TOKEN_INVALID: platform WhatsApp token rejected by Meta "
            "(phone_number_id=%s): %s",
            phone_number_id, info.last_error,
        )
    else:
        logger.error(
            "Meta API error: phone_number_id=%s, retryable=%s, %s",
            phone_number_id, info.retryable, info.last_error,
        )


def is_outside_service_window(info: MetaErrorInfo | None) -> bool:
    return info is not None and (
        info.code == OUTSIDE_SERVICE_WINDOW or info.subcode == OUTSIDE_SERVICE_WINDOW
    )


def is_rate_limited(info: MetaErrorInfo | None) -> bool:
    return info is not None and (
        info.code in RATE_LIMIT_CODES or info.subcode in RATE_LIMIT_CODES
    )
