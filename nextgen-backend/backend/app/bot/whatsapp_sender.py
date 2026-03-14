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
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from shared.models.audit import MessageOutbox
from shared.models.message import Message

logger = logging.getLogger("nextgen.bot.sender")

_settings = None


def _get_settings():
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def _masked_token(token: str) -> str:
    """Mask token for safe logging: show first 6 and last 4 chars only."""
    if len(token) <= 12:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


# ── Core send function ──────────────────────────────────────────────────────

async def send_whatsapp_message(
    phone_number_id: str,
    payload: dict,
) -> dict:
    """
    Send a message to WhatsApp via Meta Cloud API v22.0.

    Uses the platform token from WHATSAPP_DEFAULT_ACCESS_TOKEN env var.
    Token is NEVER passed per-business and NEVER logged.

    Args:
        phone_number_id: The business's WhatsApp phone number ID
        payload: Full message payload (text, template, interactive, etc.)

    Returns:
        dict with either {"success": True, "wa_message_id": "wamid.xxx"}
        or {"success": False, "error": ..., "status_code": ...}
    """
    settings = _get_settings()
    url = (
        f"{settings.META_API_BASE_URL}/{settings.META_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_DEFAULT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Ensure messaging_product is set
    payload.setdefault("messaging_product", "whatsapp")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                messages = data.get("messages", [])
                wa_message_id = messages[0].get("id") if messages else None
                logger.info(
                    "WhatsApp message sent: phone_number_id=%s, wamid=%s",
                    phone_number_id, wa_message_id,
                )
                return {"success": True, "wa_message_id": wa_message_id, "raw": data}
            else:
                error_body = resp.text[:500]
                logger.error(
                    "Meta API error: status=%d, phone_number_id=%s, body=%s",
                    resp.status_code, phone_number_id, error_body,
                )
                return {
                    "success": False,
                    "error": error_body,
                    "status_code": resp.status_code,
                }
    except httpx.TimeoutException:
        logger.error("Meta API timeout: phone_number_id=%s", phone_number_id)
        return {"success": False, "error": "timeout", "status_code": 408}
    except Exception as e:
        logger.exception("Unexpected error sending WhatsApp message")
        return {"success": False, "error": str(e), "status_code": 500}


# ── Convenience: send text + persist to DB + outbox ──────────────────────────

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
    Send a text message via WhatsApp and persist to DB + outbox.

    Model 1: NO access_token parameter. Uses platform token from env.

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

    result = await send_whatsapp_message(phone_number_id, payload)

    if result["success"]:
        wa_message_id = result.get("wa_message_id")
        outbound.wa_message_id = wa_message_id
        outbox_entry.status = "SENT"
        outbox_entry.sent_wa_message_id = wa_message_id
        outbox_entry.sent_at = datetime.now(timezone.utc)
        return wa_message_id
    else:
        outbox_entry.status = "PENDING"
        outbox_entry.attempts = 1
        outbox_entry.last_error = result.get("error", "unknown")[:500]
        logger.warning(
            "Message send failed, queued for retry: business=%s, error=%s",
            business_id, result.get("error", "unknown")[:100],
        )
        return None


# ── Template messages ────────────────────────────────────────────────────────

async def send_template_message(
    phone_number_id: str,
    recipient_wa_id: str,
    template_name: str,
    language_code: str = "en_US",
    components: list[dict] | None = None,
) -> dict:
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

    return await send_whatsapp_message(phone_number_id, payload)


# ── Interactive button messages ──────────────────────────────────────────────

async def send_interactive_buttons(
    phone_number_id: str,
    recipient_wa_id: str,
    body_text: str,
    buttons: list[dict],  # [{"id": "btn_1", "title": "Yes"}, ...]
) -> dict:
    """
    Send an interactive button message (max 3 buttons).
    Useful for order confirmation: [Confirm] [Edit] [Cancel]

    Model 1: NO access_token parameter. Uses platform token from env.
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

    return await send_whatsapp_message(phone_number_id, payload)
