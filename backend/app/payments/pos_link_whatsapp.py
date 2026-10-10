"""
POS Pay-by-Link WhatsApp delivery.

Sends a `POSPaymentLinkIntent`'s payment link to the customer via an
APPROVED WhatsApp template — the identical 24-hour-customer-service-window
problem `backend/app/receipts/whatsapp_receipt.py::send_digital_receipt`
already solves for digital receipts (Meta only allows messaging a customer
outside that window via a pre-approved template, never free text). See that
module's own docstring and `docs/SPLIT_PAYMENTS_CONTRACT.md`'s "WhatsApp
delivery" section for the locked design this mirrors.

CREDENTIAL RESOLUTION — reused, not re-implemented
----------------------------------------------------
This module never calls `backend/app/whatsapp/token_resolver.py::
resolve_whatsapp_credentials` directly, and that is deliberate: neither does
`send_digital_receipt`. Both modules resolve the SAME way — read
`business.whatsapp_phone_number_id` as the routing number (the "can this
business send WhatsApp at all" check), then pass `db=` + `business=` into
`backend/app/bot/whatsapp_sender.py::send_template_message`, which routes
through `_send_via_business_connection` -> `resolve_whatsapp_credentials`
internally. That is the one and only place the per-business
customer-owned-connection-vs-platform-token decision is made; duplicating it
here would risk silently drifting from Model 1 / DECISION-0022's fallback
rule.

TRANSACTION BOUNDARY
---------------------
Unlike `send_digital_receipt` — which owns `DigitalReceipt`'s full
get-or-create + row-lock + single-commit lifecycle because IT creates that
row and must guarantee "exactly one send per order" under concurrency —
this module does not create or lock `POSPaymentLinkIntent` rows. The caller
(`routes_pos.py`, built in parallel on this branch) already owns that row
within its own request transaction (it created the intent, obtained
`payment_link_url` from the payment provider, and will commit). This
function therefore only MUTATES `intent` in place and flushes (never
commits) — committing here would silently commit whatever else is pending
in the caller's session, which is not this function's business. Call
`await db.commit()` from the caller after this returns.

TEMPLATE_REQUIRED CLASSIFICATION — best-effort, UNVERIFIED
-------------------------------------------------------------
Meta's exact error-code taxonomy for "this template name/language is not
approved for this WABA" was not confirmed against a live Meta
sandbox/account in this session. Code 132001 ("template name does not exist
in the translation") is documented publicly as the primary code for this
condition; `_is_template_error` also falls back to a message-text check for
any other shape Meta might return it in. Treat this as a best-effort
classifier a future session should tighten with real observed Meta
responses, not as a confirmed-correct mapping — see
`backend/app/core/config.py`'s own PLACEHOLDER-template-name caveat, which
applies here for the same reason (whether `settings.
POS_PAYMENT_LINK_TEMPLATE_NAME` is actually Meta-approved for any given WABA
is unverified).

Never raises for an expected failure (no WhatsApp configured, no customer
phone, a classified Meta error) — each sets `intent.whatsapp_status` to
FAILED/TEMPLATE_REQUIRED with a reason instead, mirroring
`send_digital_receipt`'s own "never claim Sent unless Meta actually
confirmed acceptance" discipline. A genuinely unexpected error (e.g. a DB
failure) is allowed to propagate, same as that module.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.core.config import get_settings
from backend.app.payments.messages import build_pos_payment_request_message
from shared.models.business import Business
from shared.models.pos_payment import POSPaymentLinkIntent

logger = logging.getLogger("nextgen.payments.pos_link_whatsapp")
settings = get_settings()

# Best-effort / UNVERIFIED (see module docstring): Meta error codes/subcodes
# documented publicly as meaning "this template name/language is not
# approved for this WABA", as distinct from every other send failure.
_TEMPLATE_ERROR_CODES = frozenset({132001})
# Message-text fallback for a shape not covered by the code list above.
_TEMPLATE_NOT_APPROVED_MARKERS = ("does not exist", "not approved", "not found")


def _masked_phone(phone: str | None) -> str:
    """
    Mask a customer phone number for safe logging: last 4 digits only,
    matching this codebase's existing `"****" + value[-4:]` convention
    (backend/app/api/v1/routes_business.py's `_hint` helper) — never log a
    customer's full phone number at any log level.
    """
    if not phone or len(phone) < 4:
        return "****"
    return "****" + phone[-4:]


def _to_wa_id(phone_e164: str) -> str:
    """Same stripping as backend/app/receipts/whatsapp_receipt.py::_to_wa_id."""
    return phone_e164[1:] if phone_e164.startswith("+") else phone_e164


def _is_template_error(info: MetaErrorInfo) -> bool:
    """See module docstring's TEMPLATE_REQUIRED CLASSIFICATION section."""
    if info.code in _TEMPLATE_ERROR_CODES or info.subcode in _TEMPLATE_ERROR_CODES:
        return True
    message = (info.message or "").lower()
    return "template" in message and any(
        marker in message for marker in _TEMPLATE_NOT_APPROVED_MARKERS
    )


async def send_pos_payment_link(
    db: AsyncSession,
    intent: POSPaymentLinkIntent,
    business: Business,
    payment_link_url: str,
) -> None:
    """
    Attempt to deliver `intent`'s payment link to `intent.customer_phone` via
    WhatsApp, mutating `intent.whatsapp_status` / `whatsapp_message_id` /
    `whatsapp_failure_reason` in place. Never raises for an expected failure
    mode and never claims "SENT" unless Meta actually confirmed acceptance
    (see module docstring). Does not commit — the caller does.
    """
    phone_number_id = getattr(business, "whatsapp_phone_number_id", None)
    if not phone_number_id:
        intent.whatsapp_status = "FAILED"
        intent.whatsapp_failure_reason = "business has no whatsapp_phone_number_id configured"
        logger.warning(
            "POS payment-link WhatsApp send skipped: business=%s has no "
            "whatsapp_phone_number_id configured",
            business.id,
        )
        return

    if not intent.customer_phone:
        intent.whatsapp_status = "FAILED"
        intent.whatsapp_failure_reason = "no customer phone number on this payment-link intent"
        logger.warning(
            "POS payment-link WhatsApp send skipped: intent=%s has no customer_phone",
            intent.id,
        )
        return

    recipient_wa_id = _to_wa_id(intent.customer_phone)

    template_name = (
        getattr(business, "whatsapp_payment_link_template_name", None)
        or settings.POS_PAYMENT_LINK_TEMPLATE_NAME
    )
    language_code = (
        getattr(business, "whatsapp_payment_link_template_language", None)
        or settings.POS_PAYMENT_LINK_TEMPLATE_LANGUAGE
    )

    message = build_pos_payment_request_message(intent, business, payment_link_url)
    # Single body-text-variable component: build_pos_payment_request_message
    # returns ONE pre-composed message string (per this feature's own
    # requirement), unlike send_digital_receipt's 3-separate-parameter
    # template — a deliberate, documented deviation from mirroring that
    # shape exactly, since the parameter COUNT genuinely differs here (see
    # messages.py::build_pos_payment_request_message's docstring). The
    # `components` list WRAPPER shape (list of one {"type": "body",
    # "parameters": [...]} dict) is unchanged from the receipt caller.
    components = [{
        "type": "body",
        "parameters": [{"type": "text", "text": message}],
    }]

    from backend.app.bot.whatsapp_sender import send_template_message

    wamid, error_info = await send_template_message(
        phone_number_id=phone_number_id,
        recipient_wa_id=recipient_wa_id,
        template_name=template_name,
        language_code=language_code,
        components=components,
        db=db,
        business=business,
        business_id=business.id,
        intent="pos_payment_link",
    )

    if wamid:
        intent.whatsapp_status = "SENT"
        intent.whatsapp_message_id = wamid
        intent.whatsapp_failure_reason = None
        logger.info(
            "POS payment-link WhatsApp sent: intent=%s, business=%s, to=%s",
            intent.id, business.id, _masked_phone(intent.customer_phone),
        )
        return

    if error_info is not None and _is_template_error(error_info):
        intent.whatsapp_status = "TEMPLATE_REQUIRED"
        intent.whatsapp_failure_reason = (
            "No approved WhatsApp payment-link template for this business"
        )
        logger.warning(
            "POS payment-link WhatsApp send needs an approved template: "
            "intent=%s, business=%s, template=%s, meta_error=%s",
            intent.id, business.id, template_name, error_info.last_error,
        )
        return

    intent.whatsapp_status = "FAILED"
    reason = error_info.last_error if error_info else "unknown error"
    intent.whatsapp_failure_reason = reason[:255]
    logger.warning(
        "POS payment-link WhatsApp send failed: intent=%s, business=%s, reason=%s",
        intent.id, business.id, intent.whatsapp_failure_reason,
    )
