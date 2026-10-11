"""
WhatsApp Message Sender — Meta Cloud API v22.0.

Model 1 Architecture:
  Uses ONE platform System User Access Token (WHATSAPP_DEFAULT_ACCESS_TOKEN)
  from environment. Each business is identified by its phone_number_id only.
  Token is NEVER stored in DB. NEVER logged.

Supports:
  - Text replies (inbound session messages)
  - Template messages (only when explicitly requested)
  - Interactive button messages

Uses outbox pattern: writes to message_outbox first, then sends.
If send fails, the outbox worker retries later.

Wave 0A / Milestone C / DECISION-0022: send_text_message() and
send_notification_message() below accept an OPTIONAL `business` (+ `db` for
the latter) parameter. When provided, the send resolves per-business
WhatsApp credentials (backend/app/whatsapp/token_resolver.py) and, if the
business has an active customer-owned connection, sends via
backend/app/whatsapp/graph_client.py instead of the platform token — with
NO fallback to the platform token on a Graph error 190 (see
_send_via_business_connection below). Every EXISTING caller that omits
`business` keeps sending on the platform token exactly as before —
zero behaviour change for callers not updated to pass it.
"""

import logging
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.bot import meta_errors
from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.core.config import get_settings
from shared.models.audit import MessageOutbox
from shared.models.message import Message

logger = logging.getLogger("nextgen.bot.sender")
settings = get_settings()


def _masked_token(token: str) -> str:
    """Mask token for safe logging: show first 6 and last 4 chars."""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


# ── Core low-level send (Model 1: uses platform token) ──────────────────────

async def _send_via_meta_api(
    phone_number_id: str,
    recipient_wa_id: str,
    payload: dict,
) -> tuple[str | None, MetaErrorInfo | None]:
    """
    Send any message payload via Meta WhatsApp Cloud API v22.0.

    Uses WHATSAPP_DEFAULT_ACCESS_TOKEN from env (platform-level).
    Returns (wamid, None) on success, or (None, MetaErrorInfo) on failure —
    the MetaErrorInfo (backend/app/bot/meta_errors.py) is what lets a
    caller (send_text_message, outbox_worker.py) distinguish a permanent
    failure (invalid token, outside the 24h service window, invalid
    recipient) from a transient one worth retrying, instead of treating
    every failure identically.
    """
    url = (
        f"{settings.META_API_BASE_URL}/{settings.META_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    token = settings.WHATSAPP_DEFAULT_ACCESS_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Ensure required field
    payload.setdefault("messaging_product", "whatsapp")

    logger.info(
        "Meta API send: url=%s, phone_number_id=%s, token=%s",
        url, phone_number_id, _masked_token(token),
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                messages = data.get("messages", [])
                if messages:
                    return messages[0].get("id"), None
                # 200 with no messages[] is itself an anomaly worth surfacing,
                # not a silent None.
                info = meta_errors.parse_meta_error(resp.status_code, resp.text)
                meta_errors.log_meta_error(info, phone_number_id)
                return None, info
            else:
                info = meta_errors.parse_meta_error(resp.status_code, resp.text)
                meta_errors.log_meta_error(info, phone_number_id)
                return None, info
    except httpx.TimeoutException:
        info = meta_errors.transport_error(f"Meta API timeout after 10s (phone_number_id={phone_number_id})")
        logger.error(info.last_error)
        return None, info
    except Exception as exc:
        info = meta_errors.transport_error(f"Unexpected error sending WhatsApp message: {exc!r}")
        logger.exception("Unexpected error sending WhatsApp message")
        return None, info


async def _send_via_meta_api_with_detail(
    phone_number_id: str,
    recipient_wa_id: str,
    payload: dict,
) -> tuple[str | None, str | None]:
    """
    Like _send_via_meta_api but also returns an error string for diagnostics.

    Returns: (wamid | None, error_detail | None)
      - On success: (wamid, None)
      - On failure: (None, human-readable error including Meta response body)
    """
    url = (
        f"{settings.META_API_BASE_URL}/{settings.META_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    token = settings.WHATSAPP_DEFAULT_ACCESS_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload.setdefault("messaging_product", "whatsapp")

    masked = _masked_token(token)
    logger.info(
        "Meta API test-send: url=%s, phone_number_id=%s, token=%s",
        url, phone_number_id, masked,
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

            logger.info(
                "Meta API response: status=%d, phone_number_id=%s, body=%s",
                resp.status_code, phone_number_id, resp.text[:500],
            )

            if resp.status_code == 200:
                data = resp.json()
                messages = data.get("messages", [])
                if messages:
                    return messages[0].get("id"), None
                return None, "Meta API returned 200 but no messages[] in response"
            else:
                try:
                    err_body = resp.json()
                    meta_error = err_body.get("error", {})
                    detail = meta_error.get("message") or resp.text[:400]
                    error_code = meta_error.get("code", "")
                    error_subcode = meta_error.get("error_subcode", "")
                    error_str = f"Meta API error {resp.status_code}"
                    if error_code:
                        error_str += f" (code={error_code}"
                        if error_subcode:
                            error_str += f", subcode={error_subcode}"
                        error_str += ")"
                    error_str += f": {detail}"
                except Exception:
                    error_str = f"Meta API error {resp.status_code}: {resp.text[:400]}"

                logger.error(
                    "Meta API error: status=%d, phone_number_id=%s, body=%s",
                    resp.status_code, phone_number_id, resp.text[:500],
                )
                return None, error_str

    except httpx.TimeoutException:
        msg = f"Meta API timeout after 10s (phone_number_id={phone_number_id})"
        logger.error(msg)
        return None, msg
    except Exception as exc:
        msg = f"Unexpected error sending WhatsApp message: {exc!r}"
        logger.exception(msg)
        return None, msg


# ── Business-connection send (Wave 0A / Milestone C) ─────────────────────────

async def _send_via_business_connection(
    db: AsyncSession,
    business,
    payload: dict,
) -> "tuple[str | None, meta_errors.MetaErrorInfo | None]":
    """
    Resolve credentials for `business` (backend/app/whatsapp/token_resolver.py)
    and send via the customer-owned connection's Graph client when one is
    CONNECTED; falls back to the existing platform-token path
    (_send_via_meta_api) only when the business has NO active connection at
    all. A business whose connection exists but is not CONNECTED gets NO send
    attempt and NO platform fallback — the platform token has no rights on a
    customer-owned WABA (see token_resolver.py's module docstring). On Graph
    error 190, marks the connection NEEDS_ATTENTION and does not retry with a
    different credential.

    Returns (wamid | None, MetaErrorInfo | None), the same contract as
    _send_via_meta_api, so callers classify every failure identically.
    """
    from backend.app.whatsapp.connection_service import get_connection, mark_invalid_token
    from backend.app.whatsapp.graph_client import GraphAPIError, MetaGraphClient
    from backend.app.whatsapp.token_resolver import resolve_whatsapp_credentials

    creds = await resolve_whatsapp_credentials(db, business)
    if creds is None:
        connection = await get_connection(db, business.id)
        if connection is not None and connection.status != "CONNECTED":
            return None, meta_errors.not_sendable(
                f"WhatsApp connection is {connection.status} — reconnect required "
                "(no platform-token fallback)"
            )
        return None, meta_errors.not_sendable("No WhatsApp credentials configured")

    if creds.source == "platform":
        return await _send_via_meta_api(
            phone_number_id=creds.phone_number_id,
            recipient_wa_id=payload.get("to", ""),
            payload=payload,
        )

    client = MetaGraphClient()
    try:
        data = await client.send_message(creds.phone_number_id, creds.access_token, payload)
    except GraphAPIError as exc:
        if exc.is_invalid_token and creds.connection_id is not None:
            connection = await get_connection(db, business.id)
            if connection is not None:
                await mark_invalid_token(
                    db, connection,
                    "Meta reports this connection's token is no longer valid "
                    "(error 190) — reconnect required.",
                )
        info = meta_errors.from_graph_error(exc)
        logger.error(
            "Meta API error (business connection): business=%s, retryable=%s, %s",
            business.id, info.retryable, info.last_error,
        )
        return None, info

    messages = data.get("messages", [])
    if messages:
        return messages[0].get("id"), None
    return None, meta_errors.transport_error(
        "Meta API returned 200 but no messages[] in response (business connection)"
    )


# ── Text message (with outbox persistence) ───────────────────────────────────

async def send_text_message(
    db: AsyncSession,
    business_id: uuid.UUID,
    customer_wa_id: str,
    phone_number_id: str,
    text: str,
    is_llm: bool = False,
    llm_tokens: int | None = None,
    llm_cost_cents: int | None = None,
    llm_provider: str | None = None,
    intent: str | None = None,
    customer_id: uuid.UUID | None = None,
    business=None,
) -> str | None:
    """
    Send a text message to a WhatsApp user.

    1. Persist outbound message to messages table
    2. Write to outbox (reliable delivery)
    3. Attempt immediate send via Meta API (platform token, unless `business`
       is passed AND has an active customer-owned connection — see
       _send_via_business_connection)
    4. If send succeeds, mark outbox as SENT
    5. If send fails, outbox worker retries later

    `business` (optional, Wave 0A / Milestone C): pass the loaded Business
    ORM row to resolve per-business WhatsApp credentials instead of always
    using the platform token. Omitted by every caller not yet updated for
    Milestone C — behaviour for those callers is unchanged.

    Returns the wa_message_id on success, None on failure.
    """
    # ── 1. Persist outbound message ──────────────────────────────────────
    outbound = Message(
        business_id=business_id,
        customer_id=customer_id,
        direction="OUTBOUND",
        text=text,
        is_llm=is_llm,
        llm_tokens=llm_tokens,
        llm_cost_cents=llm_cost_cents,
        llm_provider=llm_provider,
        intent=intent,
    )
    db.add(outbound)

    # ── 2. Write to outbox ───────────────────────────────────────────────
    outbox_entry = MessageOutbox(
        business_id=business_id,
        customer_wa_id=customer_wa_id,
        message_text=text,
        status="SENDING",
    )
    db.add(outbox_entry)
    await db.flush()

    # ── 3. Attempt immediate send (platform token from env) ──────────────
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": customer_wa_id,
        "type": "text",
        "text": {"body": text},
    }

    if business is not None:
        # Wave 0A / Milestone C — resolve per-business credentials; the
        # helper falls back to the platform token itself when the business
        # has no active connection, so `phone_number_id` above is only used
        # as-is for callers that don't pass `business`.
        wa_message_id, error_info = await _send_via_business_connection(db, business, payload)
    else:
        wa_message_id, error_info = await _send_via_meta_api(
            phone_number_id=phone_number_id,
            recipient_wa_id=customer_wa_id,
            payload=payload,
        )

    if wa_message_id:
        outbound.wa_message_id = wa_message_id
        outbox_entry.status = "SENT"
        outbox_entry.sent_wa_message_id = wa_message_id
        from shared.utils.time import utc_now
        outbox_entry.sent_at = utc_now()
        logger.info("Message sent: wa_id=%s, business=%s", wa_message_id, business_id)
    elif error_info is not None and not error_info.retryable:
        # A permanent failure (invalid token, outside the 24h service
        # window, invalid recipient) — the outbox worker's retry loop
        # could never succeed here, so don't queue it to burn attempts.
        outbox_entry.status = "FAILED"
        outbox_entry.attempts = 1
        outbox_entry.last_error = error_info.last_error
        logger.warning(
            "Message send failed permanently, not queued for retry: business=%s, %s",
            business_id, error_info.last_error,
        )
    else:
        outbox_entry.status = "PENDING"
        outbox_entry.attempts = 1
        outbox_entry.last_error = error_info.last_error if error_info else None
        logger.warning("Message send failed, queued for retry: business=%s", business_id)

    return wa_message_id


# ── Notification message (no DB persistence) ────────────────────────────────

async def send_notification_message(
    phone_number_id: str,
    recipient_wa_id: str,
    text: str,
    db: AsyncSession | None = None,
    business=None,
    *,
    order_id: uuid.UUID | None = None,
    business_id: uuid.UUID | None = None,
    order_status: str | None = None,
) -> str | None:
    """
    Send a short notification to a customer — e.g. order status updates.
    Unlike send_text_message, this does NOT persist to the messages table or outbox.
    Call fire-and-forget inside a try/except.
    Returns wamid on success, None on failure.

    `db` + `business` (optional, Wave 0A / Milestone C): pass BOTH to resolve
    per-business WhatsApp credentials instead of always using the platform
    token (see _send_via_business_connection). Omitted by every caller not
    yet updated for Milestone C — behaviour for those callers is unchanged.

    `db`/`order_id`/`business_id`/`order_status` are optional and additive
    — every existing call site keeps working unchanged with none of them.
    When ALL FOUR are supplied by a caller that already has the order (and
    business) loaded, and the failure is Meta error 131047 (outside the
    24-hour customer-service window — this business cannot free-text this
    customer right now), an OrderEvent is written so staff can see, on the
    order itself, that the customer was never reached — instead of the
    notification just silently vanishing. Deliberately takes the caller's
    already-loaded order_status/business_id rather than re-querying, so
    this never needs its own extra round trip. `business_id` defaults to
    `business.id` when only `business` is passed.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_wa_id,
        "type": "text",
        "text": {"body": text},
    }
    if db is not None and business is not None:
        wa_message_id, error_info = await _send_via_business_connection(db, business, payload)
    else:
        wa_message_id, error_info = await _send_via_meta_api(phone_number_id, recipient_wa_id, payload)

    if business_id is None and business is not None:
        business_id = getattr(business, "id", None)

    if (
        wa_message_id is None
        and db is not None
        and order_id is not None
        and business_id is not None
        and meta_errors.is_outside_service_window(error_info)
    ):
        from shared.models.order import OrderEvent

        db.add(OrderEvent(
            order_id=order_id,
            business_id=business_id,
            old_status=order_status,
            new_status=order_status or "UNKNOWN",
            changed_by_user_id=None,
            reason="Customer notification not delivered: outside WhatsApp 24-hour window",
        ))
        await db.flush()

    return wa_message_id


# ── Image message ────────────────────────────────────────────────────────────

async def send_image_message(
    phone_number_id: str,
    recipient_wa_id: str,
    image_url: str,
    caption: str | None = None,
    db: AsyncSession | None = None,
    business=None,
) -> str | None:
    """
    Send an image message via Meta WhatsApp Cloud API.
    image_url must be a publicly accessible HTTPS URL.
    Returns the wamid on success, None on failure.

    `db` + `business` (optional, Milestone C): pass BOTH to send with the
    business's own customer-owned connection when it has one (platform token
    otherwise), exactly like send_text_message.
    """
    payload: dict = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_wa_id,
        "type": "image",
        "image": {"link": image_url},
    }
    if caption:
        payload["image"]["caption"] = caption

    logger.info(
        "Sending image: phone_number_id=%s, to=%s, url=%s",
        phone_number_id, recipient_wa_id, image_url[:60],
    )
    if db is not None and business is not None:
        wa_message_id, _error_info = await _send_via_business_connection(db, business, payload)
    else:
        wa_message_id, _error_info = await _send_via_meta_api(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            payload=payload,
        )
    return wa_message_id


# ── Template message ─────────────────────────────────────────────────────────

async def send_template_message(
    phone_number_id: str,
    recipient_wa_id: str,
    template_name: str,
    language_code: str = "en_US",
    components: list[dict] | None = None,
    db: AsyncSession | None = None,
    business=None,
    business_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    intent: str | None = None,
) -> "tuple[str | None, MetaErrorInfo | None]":
    """
    Send a template message. Only use when explicitly requested — Meta only
    allows a template send outside an active 24-hour customer-service
    window, and Meta compliance requires it be pre-approved content.

    Brought up to send_text_message's maturity level (Digital Receipts,
    Commercial V1 gate, 2026-09-22):
      - `db=`/`business=` (both optional, both required together to take
        effect) route the send through the Milestone-C per-business
        connection resolver (_send_via_business_connection) exactly like
        send_text_message — omitted, this sends on the platform token as
        before, so any caller not updated for Milestone C is unaffected.
        (Found during the same-day WhatsApp connect hardening sweep to have
        been unconditionally using the platform token regardless of
        `business` before this fix — a latent bypass of the "no
        platform-token fallback for a customer-owned number" rule in
        token_resolver.py. There were no live callers of this function yet,
        so nothing was actually exploitable, but it would have silently
        reactivated the gap the moment a caller was wired up without also
        threading `business`/`db` through.)
      - Never raises — every failure is classified via meta_errors and
        returned as a MetaErrorInfo, not thrown into the caller.
      - Returns (wamid, MetaErrorInfo) instead of a bare wamid, so a caller
        can distinguish and record WHY a send failed, not just that it did.

    Deliberately does NOT write to message_outbox — there is no automatic
    background retry for a template send. The one caller that exists today
    (backend/app/receipts/whatsapp_receipt.py::send_digital_receipt) has
    its own durable, always-one-row-per-order record on DigitalReceipt
    itself (whatsapp_status/whatsapp_failure_reason), which both survives a
    FAILED attempt and is what a staff-triggered resend re-reads — adding a
    second retry-tracking mechanism on top of that would just be two
    parallel systems recording the same fact. When `db` is provided, this
    still writes a `messages` table row (conversation-history audit, same
    table every other outbound send uses) — that is a lightweight log
    entry, not a retry queue, and a failure to write it never blocks the
    return value below.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_wa_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    if db is not None and business is not None:
        wa_message_id, error_info = await _send_via_business_connection(db, business, payload)
    else:
        wa_message_id, error_info = await _send_via_meta_api(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            payload=payload,
        )

    if db is not None:
        resolved_business_id = business_id or getattr(business, "id", None)
        if resolved_business_id is not None:
            try:
                # BUG-0129: a bare try/except around this flush, with no
                # rollback, left the session's transaction aborted on any
                # failure (e.g. a unique-constraint hit) — every subsequent
                # operation on it in the SAME request (including a caller's
                # own later db.commit(), such as the POS Pay-by-Link resend
                # endpoint's) would then raise too, turning "couldn't log
                # message history" into an unrelated 500 and silently
                # discarding whatever state the caller sets afterwards
                # (e.g. intent.whatsapp_status = "SENT") even though the
                # send itself succeeded. A SAVEPOINT isolates this one
                # write's failure without discarding anything else already
                # pending in the caller's own transaction (e.g.
                # send_digital_receipt's own DigitalReceipt row).
                async with db.begin_nested():
                    db.add(Message(
                        business_id=resolved_business_id,
                        customer_id=customer_id,
                        direction="OUTBOUND",
                        text=f"[Template: {template_name}]",
                        wa_message_id=wa_message_id,
                        intent=intent,
                    ))
                    await db.flush()
            except Exception:
                logger.exception(
                    "Failed to persist message-history row for template send "
                    "(send result itself is unaffected): business=%s, template=%s",
                    resolved_business_id, template_name,
                )

    return wa_message_id, error_info


# ── Interactive buttons ──────────────────────────────────────────────────────

async def send_interactive_buttons(
    phone_number_id: str,
    recipient_wa_id: str,
    body_text: str,
    buttons: list[dict],  # [{"id": "btn_1", "title": "Yes"}, ...]
    db: AsyncSession | None = None,
    business=None,
) -> str | None:
    """
    Send an interactive button message (max 3 buttons).
    Useful for order confirmation: [Confirm] [Edit] [Cancel]

    `db` + `business` (optional, gate hardening pass, 2026-09-22): same
    resolver-routing addition as send_template_message above, for the same
    reason (found unconditionally using the platform token; no live callers
    today).
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons[:3]  # Max 3 buttons
                ]
            },
        },
    }

    if db is not None and business is not None:
        wa_message_id, _error_info = await _send_via_business_connection(db, business, payload)
    else:
        wa_message_id, _error_info = await _send_via_meta_api(
            phone_number_id=phone_number_id,
            recipient_wa_id=recipient_wa_id,
            payload=payload,
        )
    return wa_message_id
