"""
Meta WhatsApp webhook — GET (verification) + POST (inbound messages).
Signature verification, idempotency, and business routing.
Hands off to the full bot pipeline for processing.

AUTH: This router has NO authentication dependencies.
      It is intentionally public — Meta servers must reach it unauthenticated.
      Security is enforced via HMAC signature verification on POST requests.
"""

import json
import logging

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.security import verify_meta_signature
from backend.app.db.session import get_db

logger = logging.getLogger("nextgen.webhook")

# NO dependencies=[] here intentionally — this router is fully public.
router = APIRouter(prefix="/webhook", tags=["webhook"])
settings = get_settings()


@router.get("/meta")
async def verify_webhook(
    request: Request,
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    """
    Meta webhook verification endpoint (GET).

    Three cases:
      1. Meta verification: hub.mode=subscribe + matching token → return challenge
      2. Plain browser visit (no params) → return diagnostic 200
      3. hub.mode=subscribe but wrong token → return 403
    """
    logger.warning(
        "WEBHOOK_GET_ENTRY: path=%s, mode=%r, has_token=%s, has_challenge=%s, "
        "client_ip=%s",
        request.url.path,
        mode,
        bool(token),
        bool(challenge),
        request.client.host if request.client else "unknown",
    )

    # Plain browser visit — return 200 diagnostic instead of 403
    if not mode:
        logger.warning("WEBHOOK_GET_PROBE: no hub.mode — returning diagnostic 200")
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "service": "nextgen-webhook",
                "path": "/v1/webhook/meta",
                "note": "Meta WhatsApp webhook endpoint. "
                        "Send GET with hub.mode=subscribe to verify.",
            },
        )

    # Meta verification attempt
    token_matches = token == settings.META_VERIFY_TOKEN
    if mode == "subscribe" and token_matches:
        logger.warning(
            "WEBHOOK_GET_VERIFIED: Meta webhook challenge accepted"
        )
        return Response(content=challenge, media_type="text/plain")

    # Bad token
    logger.warning(
        "WEBHOOK_GET_FAILED: mode=%r, token_matches=%s, "
        "verify_token_configured=%s",
        mode,
        token_matches,
        settings.META_VERIFY_TOKEN
        not in ("CHANGE-ME", "", "your-webhook-verify-token"),
    )
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("/meta", status_code=200)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_hub_signature_256: str | None = Header(None),
):
    """
    Receive inbound WhatsApp messages from Meta (POST).
    Always returns 200 to prevent Meta retries.
    """
    logger.warning(
        "WEBHOOK_POST_ENTRY: path=%s, content_type=%s, "
        "has_signature=%s, signature_prefix=%s, env=%s, "
        "client_ip=%s",
        request.url.path,
        request.headers.get("content-type", "missing"),
        bool(x_hub_signature_256),
        (x_hub_signature_256 or "")[:16] if x_hub_signature_256 else "NONE",
        settings.ENVIRONMENT,
        request.client.host if request.client else "unknown",
    )

    body = await request.body()

    logger.warning(
        "WEBHOOK_POST_BODY: body_len=%d, body_preview=%s",
        len(body),
        body[:2000].decode("utf-8", errors="replace"),
    )

    # ── Step 1: Signature Verification ───────────────────────────────────
    if settings.ENVIRONMENT != "development":
        app_secret_ok = settings.META_APP_SECRET not in (
            "CHANGE-ME", "", "your-app-secret"
        )
        logger.warning(
            "WEBHOOK_POST_SIG_CHECK: env=%s, has_signature=%s, "
            "app_secret_configured=%s",
            settings.ENVIRONMENT,
            bool(x_hub_signature_256),
            app_secret_ok,
        )

        if not x_hub_signature_256:
            logger.warning(
                "WEBHOOK_POST_SIG_MISSING: No X-Hub-Signature-256 header"
            )
            return {"status": "signature_failed"}

        sig_valid = verify_meta_signature(body, x_hub_signature_256)
        logger.warning(
            "WEBHOOK_POST_SIG_RESULT: valid=%s, received_prefix=%s",
            sig_valid,
            x_hub_signature_256[:32] if x_hub_signature_256 else "NONE",
        )

        if not sig_valid:
            logger.warning(
                "WEBHOOK_POST_SIG_FAILED: HMAC mismatch. "
                "Verify META_APP_SECRET in Render env matches Meta App Secret. "
                "env=%s, placeholder=%s",
                settings.ENVIRONMENT,
                not app_secret_ok,
            )
            return {"status": "signature_failed"}

        logger.warning("WEBHOOK_POST_SIG_OK: HMAC signature verified")
    else:
        logger.warning(
            "WEBHOOK_POST_SIG_SKIPPED: ENVIRONMENT=development"
        )

    # ── Step 2: Parse Payload ────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except Exception as exc:
        logger.error("WEBHOOK_POST_PARSE_ERROR: %s", exc)
        return {"status": "parse_error"}

    object_type = payload.get("object")
    logger.warning(
        "WEBHOOK_POST_PARSED: object=%s, entry_count=%d",
        object_type, len(payload.get("entry", [])),
    )

    if object_type != "whatsapp_business_account":
        logger.warning(
            "WEBHOOK_POST_IGNORED: object=%r — expected whatsapp_business_account",
            object_type,
        )
        return {"status": "ignored"}

    # ── Step 3: Process each message through the bot pipeline ────────────
    from backend.app.bot.pipeline import process_inbound_message

    entries = payload.get("entry", [])
    total_messages = 0
    total_statuses = 0

    for entry_idx, entry in enumerate(entries):
        changes = entry.get("changes", [])
        for change_idx, change in enumerate(changes):
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id")
            display_phone = metadata.get("display_phone_number", "unknown")

            logger.warning(
                "WEBHOOK_POST_CHANGE: entry=%d, change=%d, "
                "phone_number_id=%s, display_phone=%s",
                entry_idx, change_idx, phone_number_id, display_phone,
            )

            if not phone_number_id:
                logger.warning(
                    "WEBHOOK_POST_NO_PHONE_ID: entry=%d, change=%d, metadata=%s",
                    entry_idx, change_idx, metadata,
                )
                continue

            contacts = value.get("contacts", [])
            contact_name = None
            if contacts:
                profile = contacts[0].get("profile", {})
                contact_name = profile.get("name")

            messages = value.get("messages", [])
            statuses = value.get("statuses", [])

            logger.warning(
                "WEBHOOK_POST_VALUE: phone_number_id=%s, "
                "message_count=%d, status_count=%d",
                phone_number_id, len(messages), len(statuses),
            )

            for msg_idx, message in enumerate(messages):
                wa_message_id = message.get("id")
                if not wa_message_id:
                    logger.warning(
                        "WEBHOOK_POST_MSG_NO_ID: msg_idx=%d", msg_idx
                    )
                    continue

                wa_id = message.get("from", "")
                msg_type = message.get("type", "text")

                msg_text = ""
                if msg_type == "text":
                    msg_text = message.get("text", {}).get("body", "")
                elif msg_type == "interactive":
                    interactive = message.get("interactive", {})
                    if interactive.get("type") == "button_reply":
                        msg_text = interactive.get("button_reply", {}).get("title", "")
                    elif interactive.get("type") == "list_reply":
                        msg_text = interactive.get("list_reply", {}).get("title", "")

                logger.warning(
                    "WEBHOOK_POST_MESSAGE: wa_message_id=%s, wa_id=%s, "
                    "type=%s, has_text=%s, text_len=%d",
                    wa_message_id, wa_id, msg_type,
                    bool(msg_text), len(msg_text),
                )

                if not msg_text:
                    logger.warning(
                        "WEBHOOK_POST_SKIP_NONTEXT: wa_message_id=%s, type=%s",
                        wa_message_id, msg_type,
                    )
                    continue

                total_messages += 1
                logger.warning(
                    "WEBHOOK_POST_PIPELINE_START: wa_message_id=%s, "
                    "wa_id=%s, phone_number_id=%s",
                    wa_message_id, wa_id, phone_number_id,
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
                    logger.warning(
                        "WEBHOOK_POST_PIPELINE_DONE: wa_message_id=%s, wa_id=%s",
                        wa_message_id, wa_id,
                    )
                except Exception:
                    logger.exception(
                        "WEBHOOK_POST_PIPELINE_ERROR: wa_message_id=%s, wa_id=%s",
                        wa_message_id, wa_id,
                    )

            for wa_status in statuses:
                total_statuses += 1
                _handle_status_update(wa_status)

    logger.warning(
        "WEBHOOK_POST_COMPLETE: total_messages=%d, total_statuses=%d",
        total_messages, total_statuses,
    )
    return {"status": "processed"}


def _handle_status_update(status_data: dict) -> None:
    """Log delivery status updates."""
    status_val = status_data.get("status", "")
    recipient = status_data.get("recipient_id", "")
    msg_id = status_data.get("id", "")

    if status_val == "failed":
        errors = status_data.get("errors", [])
        error_msg = errors[0].get("message", "unknown") if errors else "unknown"
        logger.warning(
            "WEBHOOK_STATUS_FAILED: msg=%s, to=%s, error=%s",
            msg_id, recipient, error_msg,
        )
    else:
        logger.info(
            "WEBHOOK_STATUS: status=%s, msg=%s", status_val, msg_id
        )
