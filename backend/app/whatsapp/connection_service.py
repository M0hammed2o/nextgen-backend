"""
Orchestrates the Meta Embedded Signup connect flow end-to-end: (pre-check
the number isn't already claimed) -> code exchange -> verify claimed assets
-> subscribe webhooks -> register phone -> claim the business's inbound-
routing number. Every Graph-touching step is individually idempotent or
tolerant of being re-run (subscribe_app/register_phone/GET calls are all
safe to repeat), so calling run_exchange() again after a partial failure
simply continues making progress from where the connection row's fields
show it left off — no separate "resume" code path is needed. Every step
commits its own progress so GET /connection reflects exactly how far the
flow got even if the browser never calls exchange again.

Also implements health-check (re-read phone/WABA status) and disconnect
(best-effort unsubscribe, wipe token, clear routing number only if it
still matches this connection).
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.crypto import CredentialDecryptionError, decrypt_credential, encrypt_credential
from backend.app.whatsapp.graph_client import GraphAPIError, MetaGraphClient
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection

logger = logging.getLogger("nextgen.whatsapp.connection_service")

# The four Graph-exchange steps named in the task brief. "claim_routing_number"
# is a fifth, DB-only outcome distinct from these — a 409 conflict with
# another NextGen tenant, never a Graph API failure — kept as its own label
# so the UI can render it differently (see routes_whatsapp_connect.py).
EXCHANGE_STEPS = ("code_exchange", "verify_assets", "subscribe_webhooks", "register_phone")


class ConnectionNotFound(LookupError):
    """No whatsapp_connections row exists for this business."""


def _is_unique_violation(exc: IntegrityError, needle: str) -> bool:
    """
    Driver-agnostic unique-violation check against the DB error text —
    matches the pattern already used in routes_pos.py's
    `_is_unique_violation`. Substring-based (not an exact constraint name)
    because businesses.whatsapp_phone_number_id's unique constraint was
    created inline (shared/migrations/versions/0001_initial_schema.py) and
    picks up Postgres's own default name, not our naming convention.
    """
    return needle in str(getattr(exc, "orig", exc))


def generate_registration_pin() -> str:
    """Server-generated 6-digit PIN for POST /{phone_number_id}/register."""
    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass
class ExchangeOutcome:
    success: bool
    connection: WhatsAppConnection
    failed_step: str | None = None
    message: str = ""


async def get_connection(db: AsyncSession, business_id) -> WhatsAppConnection | None:
    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business_id)
    )
    return result.scalar_one_or_none()


async def _get_or_create_connection(db: AsyncSession, business_id) -> WhatsAppConnection:
    connection = await get_connection(db, business_id)
    if connection is None:
        connection = WhatsAppConnection(
            business_id=business_id, waba_id="", phone_number_id="", status="PENDING"
        )
        db.add(connection)
        await db.flush()
    return connection


async def _fail(db: AsyncSession, connection: WhatsAppConnection, step: str, message: str) -> ExchangeOutcome:
    connection.status = "NEEDS_ATTENTION"
    connection.status_reason = message
    await db.commit()
    await db.refresh(connection)
    logger.warning(
        "WhatsApp connect step failed: business=%s step=%s reason=%s",
        connection.business_id, step, message,
    )
    return ExchangeOutcome(False, connection, step, message)


async def run_exchange(
    db: AsyncSession,
    business: Business,
    *,
    code: str,
    waba_id: str,
    phone_number_id: str,
    connected_by_user_id,
    graph_client: MetaGraphClient | None = None,
) -> ExchangeOutcome:
    """
    Run the full connect flow for `business`. Commits progress after each
    successful step. Returns an ExchangeOutcome whose `failed_step` is one
    of EXCHANGE_STEPS or "claim_routing_number" on failure.
    """
    client = graph_client or MetaGraphClient()

    # ── Pre-check: is this Meta phone number already claimed by a
    #    DIFFERENT NextGen business? Pure DB check, no Graph call, so a
    #    doomed exchange never burns the ~30s-TTL authorization code. ────
    conflict = await db.execute(
        select(Business.id).where(
            Business.whatsapp_phone_number_id == phone_number_id,
            Business.id != business.id,
        )
    )
    if conflict.scalar_one_or_none() is not None:
        connection = await _get_or_create_connection(db, business.id)
        return await _fail(
            db, connection, "claim_routing_number",
            "This WhatsApp number is already connected to a different NextGen "
            "business. Disconnect it there first, or choose a different number.",
        )

    connection = await _get_or_create_connection(db, business.id)
    connection.waba_id = waba_id
    connection.phone_number_id = phone_number_id
    connection.status = "PENDING"
    connection.status_reason = None
    await db.commit()
    await db.refresh(connection)

    # ── Step 1: code exchange ────────────────────────────────────────────
    try:
        token_data = await client.exchange_code_for_token(code)
    except GraphAPIError as exc:
        return await _fail(
            db, connection, "code_exchange",
            f"Could not exchange the authorization code with Meta: {exc}",
        )
    access_token = token_data.get("access_token")
    if not access_token:
        return await _fail(
            db, connection, "code_exchange",
            "Meta's code-exchange response did not include an access_token.",
        )

    connection.access_token_encrypted = encrypt_credential(access_token)
    expires_in = token_data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    else:
        connection.token_expires_at = None
    await db.commit()
    await db.refresh(connection)

    # ── Step 2: verify claimed assets — never trust the browser's ids ──
    try:
        waba_data = await client.get_waba(waba_id, access_token)
        phone_data = await client.get_phone_number(phone_number_id, access_token)
    except GraphAPIError as exc:
        detail = "the token does not have access to it (Graph error 190)" if exc.is_invalid_token else str(exc)
        return await _fail(
            db, connection, "verify_assets",
            f"Could not confirm this Meta account actually owns the selected "
            f"WhatsApp number/account — {detail}. Nothing was connected.",
        )
    if waba_data.get("id") != waba_id:
        return await _fail(
            db, connection, "verify_assets",
            "The WhatsApp Business Account Meta returned did not match the one selected.",
        )

    connection.display_phone_number = phone_data.get("display_phone_number")
    connection.verified_name = phone_data.get("verified_name")
    connection.quality_rating = phone_data.get("quality_rating")
    await db.commit()
    await db.refresh(connection)

    # ── Step 3: subscribe our app's webhooks to this WABA ────────────────
    try:
        await client.subscribe_app(waba_id, access_token)
    except GraphAPIError as exc:
        return await _fail(
            db, connection, "subscribe_webhooks",
            f"Could not subscribe to WhatsApp notifications for this account: {exc}",
        )
    connection.webhook_subscribed = True
    await db.commit()
    await db.refresh(connection)

    # ── Step 4: register the phone number for Cloud API messaging ───────
    pin = generate_registration_pin()
    try:
        await client.register_phone(phone_number_id, access_token, pin)
    except GraphAPIError as exc:
        if not exc.is_already_registered:
            return await _fail(
                db, connection, "register_phone",
                f"Could not register this phone number for messaging: {exc}",
            )
        logger.info("Phone %s already registered — treating as success.", phone_number_id)
    connection.registration_pin_encrypted = encrypt_credential(pin)
    connection.phone_registered = True
    await db.commit()
    await db.refresh(connection)

    # ── Step 5 (DB-only): claim the business's inbound-routing number ───
    return await _claim_routing_number(db, business, connection, connected_by_user_id)


async def _claim_routing_number(
    db: AsyncSession, business: Business, connection: WhatsAppConnection, connected_by_user_id
) -> ExchangeOutcome:
    existing = await db.execute(
        select(Business.id).where(
            Business.whatsapp_phone_number_id == connection.phone_number_id,
            Business.id != business.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return await _fail(
            db, connection, "claim_routing_number",
            "This WhatsApp number is already connected to a different NextGen "
            "business. Disconnect it there first, or choose a different number.",
        )

    business.whatsapp_phone_number_id = connection.phone_number_id
    business.whatsapp_business_account_id = connection.waba_id
    connection.status = "CONNECTED"
    connection.status_reason = None
    connection.connected_at = datetime.now(timezone.utc)
    connection.disconnected_at = None
    connection.connected_by_user_id = connected_by_user_id
    connection.last_health_check_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_unique_violation(exc, "whatsapp_phone_number_id") or _is_unique_violation(exc, "phone_number_id"):
            await db.refresh(connection)
            connection.status = "NEEDS_ATTENTION"
            connection.status_reason = (
                "This WhatsApp number is already connected to a different NextGen business."
            )
            await db.commit()
            return ExchangeOutcome(False, connection, "claim_routing_number", connection.status_reason)
        raise
    await db.refresh(connection)
    return ExchangeOutcome(True, connection, None, "WhatsApp connected successfully.")


async def mark_invalid_token(db: AsyncSession, connection: WhatsAppConnection, detail: str) -> None:
    """
    Mark a connection NEEDS_ATTENTION after a Graph error code 190 (invalid/
    expired token) surfaces on an actual send — shared by
    backend/app/bot/whatsapp_sender.py's business-connection send path and
    routes_whatsapp_connect.py's POST /test-message.
    """
    connection.status = "NEEDS_ATTENTION"
    connection.status_reason = detail
    await db.commit()
    await db.refresh(connection)


async def disconnect(
    db: AsyncSession, business: Business, graph_client: MetaGraphClient | None = None
) -> WhatsAppConnection:
    """
    Best-effort unsubscribe from Meta, then always wipe local credentials
    and clear the business's routing number (only if it still points at
    this connection) regardless of whether the Meta-side call succeeded —
    the owner asked to disconnect; a live local token/webhook subscription
    is a bigger risk than a possibly-orphaned Meta-side subscription.
    """
    connection = await get_connection(db, business.id)
    if connection is None:
        raise ConnectionNotFound(f"No WhatsApp connection exists for business {business.id}")

    client = graph_client or MetaGraphClient()
    if connection.access_token_encrypted and connection.waba_id:
        try:
            token = decrypt_credential(connection.access_token_encrypted)
            if token:
                await client.unsubscribe_app(connection.waba_id, token)
        except (GraphAPIError, CredentialDecryptionError) as exc:
            logger.warning(
                "WhatsApp disconnect: unsubscribe_app failed for business=%s (proceeding "
                "with local disconnect anyway): %s", business.id, exc,
            )

    if business.whatsapp_phone_number_id == connection.phone_number_id:
        business.whatsapp_phone_number_id = None

    connection.status = "DISCONNECTED"
    connection.status_reason = "Disconnected by owner"
    connection.access_token_encrypted = None
    connection.registration_pin_encrypted = None
    connection.webhook_subscribed = False
    connection.phone_registered = False
    connection.disconnected_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(connection)
    return connection


async def health_check(
    db: AsyncSession, business: Business, graph_client: MetaGraphClient | None = None
) -> tuple[WhatsAppConnection, dict]:
    """
    Re-read phone/WABA status from Meta and update the connection row.

    Returns (connection, result) where result is:
      {"ok": bool, "detail": "<plain English>", "technical_details": {...}}
    `technical_details` never contains a token/secret/pin — safe to display.
    """
    connection = await get_connection(db, business.id)
    if connection is None:
        raise ConnectionNotFound(f"No WhatsApp connection exists for business {business.id}")

    if connection.status == "DISCONNECTED":
        return connection, {
            "ok": False,
            "detail": "This business is not connected — nothing to check.",
            "technical_details": {},
        }

    client = graph_client or MetaGraphClient()
    token = decrypt_credential(connection.access_token_encrypted) if connection.access_token_encrypted else None
    if not token:
        connection.status = "NEEDS_ATTENTION"
        connection.status_reason = "No stored access token — reconnect required."
        connection.last_health_check_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(connection)
        return connection, {
            "ok": False, "detail": connection.status_reason, "technical_details": {},
        }

    try:
        phone_data = await client.get_phone_number(connection.phone_number_id, token)
    except GraphAPIError as exc:
        if exc.is_invalid_token:
            connection.status_reason = (
                "Meta reports this connection's token is no longer valid (error 190) — reconnect required."
            )
        else:
            connection.status_reason = f"Health check failed: {exc}"
        connection.status = "NEEDS_ATTENTION"
        connection.last_health_check_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(connection)
        return connection, {
            "ok": False,
            "detail": connection.status_reason,
            "technical_details": {"error_code": exc.error_code, "status_code": exc.status_code},
        }

    connection.display_phone_number = phone_data.get("display_phone_number", connection.display_phone_number)
    connection.verified_name = phone_data.get("verified_name", connection.verified_name)
    connection.quality_rating = phone_data.get("quality_rating")
    connection.last_health_check_at = datetime.now(timezone.utc)
    if connection.status == "NEEDS_ATTENTION":
        connection.status = "CONNECTED"
        connection.status_reason = None
    await db.commit()
    await db.refresh(connection)
    return connection, {
        "ok": True,
        "detail": "Connection healthy.",
        "technical_details": {
            "quality_rating": connection.quality_rating,
            "code_verification_status": phone_data.get("code_verification_status"),
        },
    }
