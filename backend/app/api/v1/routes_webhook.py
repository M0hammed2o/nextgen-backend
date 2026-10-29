"""
Meta WhatsApp webhook — GET (verification) + POST (inbound messages).

Model 1 compliant:
  - HMAC SHA256 signature verification using META_APP_SECRET
  - Routes by phone_number_id → business lookup
  - No full message content logged in production
  - Always returns 200 to Meta to prevent retries
  - Meta Cloud API v23.0

AUTH: This router has NO authentication dependencies.
      It is intentionally public — Meta servers must reach it unauthenticated.
      Security is enforced via HMAC signature verification on POST requests.
"""

import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.security import verify_meta_signature
from backend.app.db.session import get_db
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection

logger = logging.getLogger("nextgen.webhook")

# NO dependencies=[] here intentionally — this router is fully public.
router = APIRouter(prefix="/webhook", tags=["webhook"])
settings = get_settings()

# ── Wave 0A / Milestone C / DECISION-0022 — account_update event handling ──
# VERIFY AGAINST META DOCS: event names/payload shape researched 2026-09-21,
# not exercised against a live Meta app. entry.id in these payloads may be
# the partner's portfolio id, not the WABA id — the WABA id always comes
# from value.waba_info.waba_id per the task brief, never from entry.id.
_ACCOUNT_EVENT_OUTCOMES = {
    "PARTNER_APP_UNINSTALLED": ("DISCONNECTED", "Meta reports this app was uninstalled from the WhatsApp Business Account."),
    "PARTNER_REMOVED": ("DISCONNECTED", "Meta reports this app's access to the WhatsApp Business Account was removed."),
    "ACCOUNT_DELETED": ("DISCONNECTED", "Meta reports this WhatsApp Business Account was deleted."),
    "DISABLED_UPDATE": ("NEEDS_ATTENTION", "Meta reports this WhatsApp Business Account was disabled."),
    "ACCOUNT_VIOLATION": ("NEEDS_ATTENTION", "Meta reports a policy violation on this WhatsApp Business Account."),
    "ACCOUNT_RESTRICTION": ("NEEDS_ATTENTION", "Meta reports a restriction on this WhatsApp Business Account."),
}


async def _stamp_last_webhook(db: AsyncSession, phone_number_id: str) -> None:
    """
    Best-effort: record that a webhook was just received for this
    phone_number_id, on whatsapp_connections.last_webhook_at (Wave 0A /
    Milestone C) and the pre-existing Business.last_webhook_received_at
    column. If no connection row exists, the first UPDATE simply matches
    zero rows — never an error. Wrapped so ANY failure here can never slow
    or break inbound message handling or the 200 response Meta needs.
    """
    try:
        now = datetime.now(timezone.utc)
        await db.execute(
            sa_update(WhatsAppConnection)
            .where(WhatsAppConnection.phone_number_id == phone_number_id)
            .values(last_webhook_at=now)
        )
        await db.execute(
            sa_update(Business)
            .where(Business.whatsapp_phone_number_id == phone_number_id)
            .values(last_webhook_received_at=now)
        )
        await db.commit()
    except Exception:
        logger.exception("WEBHOOK_STAMP_FAILED (non-fatal): phone_number_id=%s", phone_number_id)
        try:
            await db.rollback()
        except Exception:
            pass


async def _handle_account_update_event(db: AsyncSession, value: dict) -> None:
    """
    React to an account_update webhook change (Wave 0A / Milestone C) — an
    account-lifecycle event, distinct from an ordinary message/status.
    Best-effort: any failure here is logged, never raised.
    """
    try:
        event = value.get("event")
        waba_id = (value.get("waba_info") or {}).get("waba_id")
        if not event or not waba_id:
            logger.info("WEBHOOK_ACCOUNT_EVENT_MISSING_FIELDS: value=%s", value)
            return
        outcome = _ACCOUNT_EVENT_OUTCOMES.get(event)
        if outcome is None:
            logger.info("WEBHOOK_ACCOUNT_EVENT_UNHANDLED: event=%s waba_id=%s", event, waba_id)
            return
        new_status, reason = outcome

        result = await db.execute(
            select(WhatsAppConnection).where(WhatsAppConnection.waba_id == waba_id)
        )
        try:
            connection = result.scalar_one_or_none()
        except MultipleResultsFound:
            # Should be impossible after migration 0037's partial UNIQUE
            # index on waba_id, but never silently drop the event if it
            # somehow happens (e.g. against a DB not yet migrated) — log
            # loudly with the waba_id and every matching connection id
            # rather than letting the broad except below swallow it as a
            # generic, unidentifiable error.
            dup_ids = (
                await db.execute(
                    select(WhatsAppConnection.id).where(WhatsAppConnection.waba_id == waba_id)
                )
            ).scalars().all()
            logger.error(
                "WEBHOOK_ACCOUNT_EVENT_DUPLICATE_WABA: waba_id=%s event=%s matches "
                "multiple connections=%s — refusing to guess which one.",
                waba_id, event, dup_ids,
            )
            return
        if connection is None:
            logger.info("WEBHOOK_ACCOUNT_EVENT_NO_CONNECTION: event=%s waba_id=%s", event, waba_id)
            return

        connection.status = new_status
        connection.status_reason = reason
        if new_status == "DISCONNECTED":
            connection.disconnected_at = datetime.now(timezone.utc)
            connection.access_token_encrypted = None
            connection.registration_pin_encrypted = None
        await db.commit()
        logger.warning(
            "WEBHOOK_ACCOUNT_EVENT_APPLIED: event=%s waba_id=%s business=%s new_status=%s",
            event, waba_id, connection.business_id, new_status,
        )
    except Exception:
        logger.exception("WEBHOOK_ACCOUNT_EVENT_ERROR (non-fatal)")
        try:
            await db.rollback()
        except Exception:
            pass


@router.get("/meta")
async def verify_webhook(
    request: Request,
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
) -> Response:
    """
    Meta webhook verification endpoint (GET).

    Two cases:
      1. Meta verification call: hub.mode=subscribe + hub.verify_token + hub.challenge
         → return the challenge as plain text (200 OK)
      2. Plain browser/health-check visit (no query params)
         → return a diagnostic 200 JSON (not 403)
      3. hub.mode=subscribe but wrong token
         → return 403 (actual verification failure)
    """
    # ── ENTRY — WARNING level so it appears at any Render log setting ─────
    logger.warning(
        "WEBHOOK_GET_ENTRY: path=%s, mode=%r, has_token=%s, has_challenge=%s, "
        "client_ip=%s",
        request.url.path,
        mode,
        bool(token),
        bool(challenge),
        request.client.host if request.client else "unknown",
    )

    # ── Case 2: plain browser/health visit (no Meta params) ──────────────
    # Return 200 so the endpoint is not mistaken for broken.
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

    # ── Case 1: Meta verification ─────────────────────────────────────────
    # Constant-time comparison — matches the pattern used for every other
    # provider signature/secret check in this codebase (see SECURITY.md).
    token_matches = hmac.compare_digest(token, settings.META_VERIFY_TOKEN)
    if mode == "subscribe" and token_matches:
        logger.warning(
            "WEBHOOK_GET_VERIFIED: Meta webhook challenge accepted, returning challenge"
        )
        return Response(content=challenge, media_type="text/plain")

    # ── Case 3: Meta sent subscribe but token is wrong ────────────────────
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
    x_hub_signature_256: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Receive inbound WhatsApp messages from Meta (POST).

    Pipeline:
    1. Log entry (WARNING level — always visible)
    2. Verify HMAC SHA256 signature (X-Hub-Signature-256)
    3. Parse payload → extract phone_number_id for business routing
    4. Enqueue each message as an ARQ job — returns 200 immediately

    Always returns 200 to Meta (even on internal errors) to prevent retries.
    Processing happens asynchronously in the ARQ worker process.
    """
    # ── ENTRY — WARNING level so it appears at any Render log setting ─────
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

    # Log size at WARNING so it's always visible; full body only at DEBUG
    # to avoid customer message content appearing in production log streams.
    logger.warning("WEBHOOK_POST_BODY: body_len=%d", len(body))
    logger.debug(
        "WEBHOOK_POST_BODY_DETAIL: %s",
        body[:500].decode("utf-8", errors="replace"),
    )

    # ── Step 1: HMAC Signature Verification ──────────────────────────────
    # Skip in development for local testing. Always verify in production.
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
                "WEBHOOK_POST_SIG_MISSING: No X-Hub-Signature-256 header — "
                "returning signature_failed (200 to prevent Meta retries)"
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
                "Check META_APP_SECRET in Render env vars matches "
                "Meta App Dashboard → App Settings → Basic → App Secret. "
                "env=%s, app_secret_is_placeholder=%s",
                settings.ENVIRONMENT,
                not app_secret_ok,
            )
            return {"status": "signature_failed"}

        logger.warning("WEBHOOK_POST_SIG_OK: HMAC signature verified")
    else:
        logger.warning(
            "WEBHOOK_POST_SIG_SKIPPED: ENVIRONMENT=development, "
            "skipping signature verification"
        )

    # ── Step 2: Parse Payload ────────────────────────────────────────────
    try:
        payload = json.loads(body)
    except Exception as exc:
        logger.error(
            "WEBHOOK_POST_PARSE_ERROR: Failed to parse JSON body: %s", exc
        )
        return {"status": "parse_error"}

    object_type = payload.get("object")
    entry_count = len(payload.get("entry", []))
    logger.warning(
        "WEBHOOK_POST_PARSED: object=%s, entry_count=%d",
        object_type, entry_count,
    )

    if object_type != "whatsapp_business_account":
        logger.warning(
            "WEBHOOK_POST_IGNORED: object=%r — expected 'whatsapp_business_account'",
            object_type,
        )
        return {"status": "ignored"}

    # ── Step 3: Enqueue each message as an ARQ job ───────────────────────
    entries = payload.get("entry", [])
    total_messages = 0
    total_statuses = 0
    enqueue_failed = False

    for entry_idx, entry in enumerate(entries):
        changes = entry.get("changes", [])
        for change_idx, change in enumerate(changes):
            value = change.get("value", {})

            # ── Wave 0A / Milestone C: account-level event, not a message —
            #    handled separately and never falls through to message
            #    processing below (no metadata.phone_number_id to route on).
            if change.get("field") == "account_update":
                await _handle_account_update_event(db, value)
                continue

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
                    "WEBHOOK_POST_NO_PHONE_ID: entry=%d, change=%d, "
                    "metadata=%s — skipping change",
                    entry_idx, change_idx, metadata,
                )
                continue

            # Wave 0A / Milestone C — best-effort, never blocks/breaks
            # message handling below (see _stamp_last_webhook docstring).
            await _stamp_last_webhook(db, phone_number_id)

            # Fail-closed WhatsApp routing binding (migration 0059): resolve
            # the business and its current routing generation ONCE, at
            # webhook-accept time, and snapshot both into every job enqueued
            # for this change. The worker (pipeline.py::_process) re-checks
            # this snapshot against the business's CURRENT generation before
            # processing — if a number reassignment happened in between, the
            # snapshot is stale and the job is refused rather than silently
            # processed under the new owner. A change with no matching
            # business at all is not enqueued, not even a quarantine: there
            # is nothing to quarantine it as, so the message is simply
            # dropped here, logged loudly, same as the previous "no business
            # found" behaviour used to produce deeper inside the pipeline —
            # just detected one step earlier now.
            #
            # If the lookup itself fails (DB hiccup, not "no business"), this
            # must behave like _stamp_last_webhook just above it: never let an
            # infra blip drop real messages. Fall back to enqueueing with no
            # snapshot — pipeline.py's own fallback path then behaves exactly
            # as it did before migration 0059. Only a SUCCESSFUL lookup that
            # genuinely finds nothing skips enqueueing.
            expected_business_id = None
            expected_routing_generation = None
            routing_lookup_succeeded = True
            try:
                routing_result = await db.execute(
                    select(Business.id, Business.whatsapp_routing_generation).where(
                        Business.whatsapp_phone_number_id == phone_number_id
                    )
                )
                routing_row = routing_result.first()
            except Exception:
                logger.exception(
                    "WEBHOOK_ROUTING_LOOKUP_FAILED (non-fatal): phone_number_id=%s — "
                    "falling back to legacy enqueue without a routing snapshot",
                    phone_number_id,
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                routing_lookup_succeeded = False
                routing_row = None

            if routing_lookup_succeeded and routing_row is None:
                logger.warning(
                    "WEBHOOK_POST_NO_BUSINESS_FOR_NUMBER: phone_number_id=%s — "
                    "no business currently owns this number, dropping change without enqueueing",
                    phone_number_id,
                )
                continue
            if routing_lookup_succeeded:
                expected_business_id, expected_routing_generation = routing_row

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
                        "WEBHOOK_POST_MSG_NO_ID: msg_idx=%d, message=%s",
                        msg_idx, message,
                    )
                    continue

                wa_id = message.get("from", "")
                msg_type = message.get("type", "text")

                # Extract text based on message type
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
                        "WEBHOOK_POST_SKIP_NONTEXT: wa_message_id=%s, type=%s — "
                        "no extractable text, skipping pipeline",
                        wa_message_id, msg_type,
                    )
                    continue

                total_messages += 1
                logger.warning(
                    "WEBHOOK_POST_ENQUEUE: wa_message_id=%s, "
                    "wa_id=%s, phone_number_id=%s",
                    wa_message_id, wa_id, phone_number_id,
                )

                try:
                    await request.app.state.arq_pool.enqueue_job(
                        "process_whatsapp_message",
                        phone_number_id,
                        wa_message_id,
                        wa_id,
                        msg_text,
                        msg_type,
                        message,
                        contact_name,
                        expected_business_id,
                        expected_routing_generation,
                    )
                    logger.warning(
                        "WEBHOOK_POST_ENQUEUED: wa_message_id=%s, wa_id=%s",
                        wa_message_id, wa_id,
                    )
                except Exception:
                    enqueue_failed = True
                    logger.exception(
                        "WEBHOOK_POST_ENQUEUE_ERROR: wa_message_id=%s, wa_id=%s — "
                        "message was NOT queued; check ARQ pool health.",
                        wa_message_id, wa_id,
                    )

            for wa_status in statuses:
                total_statuses += 1
                _handle_status_update(wa_status)

    logger.warning(
        "WEBHOOK_POST_COMPLETE: total_messages=%d enqueued, total_statuses=%d, "
        "enqueue_failed=%s",
        total_messages, total_statuses, enqueue_failed,
    )

    if enqueue_failed:
        # A message failed to enqueue — it was never queued for processing.
        # Return 5xx so Meta retries the whole delivery (Meta retries failed
        # webhook deliveries automatically). The pipeline de-duplicates by
        # wa_message_id, so re-processing already-enqueued messages on retry
        # is safe. Returning 200 here (the old behaviour) silently dropped
        # the customer's message forever — Meta never retries a 200.
        return JSONResponse(status_code=503, content={"status": "enqueue_failed"})

    return {"status": "ok"}


def _handle_status_update(status_data: dict) -> None:
    """Log delivery status updates (sent, delivered, read, failed)."""
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
