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
"""

import logging
import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

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
) -> str | None:
    """
    Send any message payload via Meta WhatsApp Cloud API v22.0.

    Uses WHATSAPP_DEFAULT_ACCESS_TOKEN from env (platform-level).
    Returns the wamid on success, None on failure.
    """
    url = (
        f"{settings.META_API_BASE_URL}/{settings.META_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_DEFAULT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Ensure required field
    payload.setdefault("messaging_product", "whatsapp")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                messages = data.get("messages", [])
                if messages:
                    return messages[0].get("id")
            else:
                logger.error(
                    "Meta API error: status=%d, phone_number_id=%s, body=%s",
                    resp.status_code, phone_number_id, resp.text[:500],
                )
    except httpx.TimeoutException:
        logger.error("Meta API timeout: phone_number_id=%s", phone_number_id)
    except Exception:
        logger.exception("Unexpected error sending WhatsApp message")

    return None


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
) -> str | None:
    """
    Send a text message to a WhatsApp user.

    1. Persist outbound message to messages table
    2. Write to outbox (reliable delivery)
    3. Attempt immediate send via Meta API (platform token)
    4. If send succeeds, mark outbox as SENT
    5. If send fails, outbox worker retries later

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

    wa_message_id = await _send_via_meta_api(
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
    else:
        outbox_entry.status = "PENDING"
        outbox_entry.attempts = 1
        logger.warning("Message send failed, queued for retry: business=%s", business_id)

    return wa_message_id


# ── Template message ─────────────────────────────────────────────────────────

async def send_template_message(
    phone_number_id: str,
    recipient_wa_id: str,
    template_name: str,
    language_code: str = "en_US",
    components: list[dict] | None = None,
) -> str | None:
    """
    Send a template message (e.g., hello_world).
    Only use when explicitly requested — Meta compliance.
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

    return await _send_via_meta_api(
        phone_number_id=phone_number_id,
        recipient_wa_id=recipient_wa_id,
        payload=payload,
    )


# ── Interactive buttons ──────────────────────────────────────────────────────

async def send_interactive_buttons(
    phone_number_id: str,
    recipient_wa_id: str,
    body_text: str,
    buttons: list[dict],  # [{"id": "btn_1", "title": "Yes"}, ...]
) -> str | None:
    """
    Send an interactive button message (max 3 buttons).
    Useful for order confirmation: [Confirm] [Edit] [Cancel]
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

    return await _send_via_meta_api(
        phone_number_id=phone_number_id,
        recipient_wa_id=recipient_wa_id,
        payload=payload,
    )
