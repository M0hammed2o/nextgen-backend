"""
Meta WhatsApp webhook — GET (verification) + POST (inbound messages).
Signature verification, idempotency, and business routing.
Hands off to the full bot pipeline for processing.
"""

import logging

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.security import verify_meta_signature
from backend.app.db.session import get_db

logger = logging.getLogger("nextgen.webhook")
router = APIRouter(prefix="/webhook", tags=["webhook"])
settings = get_settings()


@router.get("/meta")
async def verify_webhook(
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    """
    Meta webhook verification endpoint.
    Meta sends GET with hub.mode, hub.verify_token, hub.challenge.
    We return the challenge if the token matches.
    """
    if mode == "subscribe" and token == settings.META_VERIFY_TOKEN:
        logger.info("Meta webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("Meta webhook verification failed: mode=%s", mode)
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/meta", status_code=200)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_hub_signature_256: str | None = Header(None),
):
    """
    Receive inbound WhatsApp messages from Meta.
    
    Pipeline:
    1. Verify signature (X-Hub-Signature-256)
    2. Parse payload → extract phone_number_id for business routing
    3. Route each message to the full bot pipeline
    
    Always returns 200 to Meta (even on internal errors) to prevent retries.
    """
    body = await request.body()

    # ── Step 1: Signature Verification ───────────────────────────────────
    if settings.ENVIRONMENT != "development":
        if not x_hub_signature_256 or not verify_meta_signature(body, x_hub_signature_256):
            logger.warning("Meta webhook signature verification failed")
            return {"status": "signature_failed"}

    # ── Step 2: Parse Payload ────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        logger.error("Failed to parse webhook JSON")
        return {"status": "parse_error"}

    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    # ── Step 3: Process each message through the bot pipeline ────────────
    from backend.app.bot.pipeline import process_inbound_message

    entries = payload.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")

            if not phone_number_id:
                continue

            # Extract contact name if available
            contacts = value.get("contacts", [])
            contact_name = None
            if contacts:
                profile = contacts[0].get("profile", {})
                contact_name = profile.get("name")

            messages = value.get("messages", [])
            for message in messages:
                wa_message_id = message.get("id")
                if not wa_message_id:
                    continue

                wa_id = message.get("from", "")
                msg_type = message.get("type", "text")

                # Extract message text based on type
                msg_text = ""
                if msg_type == "text":
                    msg_text = message.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    interactive = message.get("interactive", {})
                    if interactive.get("type") == "button_reply":
                        msg_text = interactive.get("button_reply", {}).get("title", "")
                    elif interactive.get("type") == "list_reply":
                        msg_text = interactive.get("list_reply", {}).get("title", "")

                if not msg_text:
                    logger.debug("Skipping non-text message type: %s", msg_type)
                    continue

                # PRODUCTION SAFETY: Do not log message body or personal data
                if settings.ENVIRONMENT == "production":
                    logger.info(
                        "Inbound: phone_number_id=%s, wa_id=%s, type=%s",
                        phone_number_id, wa_id, msg_type,
                    )
                else:
                    logger.info(
                        "Inbound: phone_number_id=%s, wa_id=%s, type=%s, len=%d",
                        phone_number_id, wa_id, msg_type, len(msg_text),
                    )

                try:
                    await process_inbound_message(
                        db=db,
                        phone_number_id=phone_number_id,
                        wa_message_id=wa_message_id,
                        wa_id=wa_id,
                        msg_text=msg_text,
                        msg_type=msg_type,
                        raw_payload=message,
                        contact_name=contact_name,
                    )
                except Exception:
                    logger.exception(
                        "Error processing message wa_id=%s from %s",
                        wa_message_id, wa_id,
                    )

            # Handle status updates (delivery receipts)
            statuses = value.get("statuses", [])
            for wa_status in statuses:
                _handle_status_update(wa_status)

    return {"status": "processed"}


def _handle_status_update(status_data: dict) -> None:
    """Log delivery status updates (sent, delivered, read, failed)."""
    status_val = status_data.get("status", "")
    recipient = status_data.get("recipient_id", "")
    msg_id = status_data.get("id", "")

    if status_val == "failed":
        errors = status_data.get("errors", [])
        error_msg = errors[0].get("message", "unknown") if errors else "unknown"
        logger.warning(
            "WhatsApp delivery failed: msg=%s, to=%s, error=%s",
            msg_id, recipient, error_msg,
        )
    else:
        logger.debug("WhatsApp status: %s for msg=%s", status_val, msg_id)
