"""
Business-plane routes for connecting/managing a customer-owned WhatsApp
account via Meta Embedded Signup (Wave 0A / Milestone C / DECISION-0022).

Every route here is additionally gated on `Business.is_whatsapp_enabled`
(the existing WhatsApp module kill switch) — 409 otherwise, same pattern as
`_require_inventory_enabled` in routes_inventory.py.

NEVER returns access_token_encrypted/registration_pin_encrypted or any
decrypted secret in any response — see WhatsAppConnectionStatus below,
which deliberately omits them.

Permissions: connect (POST /exchange) and disconnect (DELETE) are OWNER
only — they bind/unbind the restaurant's own Meta assets. Status/health/
test-message are OWNER or MANAGER.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.crypto import CredentialDecryptionError, decrypt_credential
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.ratelimit import limiter
from backend.app.core.rbac import AuthUser, require_owner, require_owner_or_manager
from backend.app.db.session import get_db
from backend.app.whatsapp import connection_service
from backend.app.whatsapp.graph_client import GraphAPIError, MetaGraphClient
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection

logger = logging.getLogger("nextgen.whatsapp.routes")
settings = get_settings()

router = APIRouter(prefix="/business/whatsapp/connection", tags=["whatsapp-connect"])


# ── Guard (mirrors _require_inventory_enabled in routes_inventory.py) ───────

async def _require_whatsapp_module_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.is_whatsapp_enabled:
        raise AppError(
            "WHATSAPP_NOT_ENABLED",
            "WhatsApp is not enabled for this business.",
            409,
        )
    return business


# ── Schemas ──────────────────────────────────────────────────────────────────

class WhatsAppConnectionStatus(BaseModel):
    """
    Never includes access_token_encrypted/registration_pin_encrypted or any
    decrypted secret — the frontend never needs them.
    """
    connected: bool
    status: str  # PENDING | CONNECTED | NEEDS_ATTENTION | DISCONNECTED | NOT_CONNECTED
    status_reason: str | None
    waba_id: str | None
    display_phone_number: str | None
    verified_name: str | None
    webhook_subscribed: bool
    phone_registered: bool
    quality_rating: str | None
    messaging_limit_tier: str | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    last_health_check_at: datetime | None
    last_webhook_at: datetime | None
    on_legacy_platform_path: bool
    whatsapp_module_enabled: bool
    whatsapp_paused: bool


def _status_response(business: Business, connection: WhatsAppConnection | None) -> WhatsAppConnectionStatus:
    if connection is None:
        return WhatsAppConnectionStatus(
            connected=False,
            status="NOT_CONNECTED",
            status_reason=None,
            waba_id=None,
            display_phone_number=None,
            verified_name=None,
            webhook_subscribed=False,
            phone_registered=False,
            quality_rating=None,
            messaging_limit_tier=None,
            connected_at=None,
            disconnected_at=None,
            last_health_check_at=None,
            last_webhook_at=None,
            on_legacy_platform_path=bool(business.whatsapp_phone_number_id),
            whatsapp_module_enabled=business.is_whatsapp_enabled,
            whatsapp_paused=business.whatsapp_paused,
        )
    return WhatsAppConnectionStatus(
        connected=connection.status == "CONNECTED",
        status=connection.status,
        status_reason=connection.status_reason,
        waba_id=connection.waba_id or None,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        webhook_subscribed=connection.webhook_subscribed,
        phone_registered=connection.phone_registered,
        quality_rating=connection.quality_rating,
        messaging_limit_tier=connection.messaging_limit_tier,
        connected_at=connection.connected_at,
        disconnected_at=connection.disconnected_at,
        last_health_check_at=connection.last_health_check_at,
        last_webhook_at=connection.last_webhook_at,
        on_legacy_platform_path=(
            connection.status != "CONNECTED" and bool(business.whatsapp_phone_number_id)
        ),
        whatsapp_module_enabled=business.is_whatsapp_enabled,
        whatsapp_paused=business.whatsapp_paused,
    )


class WhatsAppConnectConfig(BaseModel):
    available: bool
    reason: str | None = None
    app_id: str | None = None
    config_id: str | None = None
    graph_version: str | None = None


class ExchangeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    waba_id: str = Field(min_length=1, max_length=64)
    phone_number_id: str = Field(min_length=1, max_length=64)


class ExchangeResponse(BaseModel):
    success: bool
    failed_step: str | None
    message: str
    connection: WhatsAppConnectionStatus


class HealthCheckResponse(BaseModel):
    ok: bool
    detail: str
    technical_details: dict
    connection: WhatsAppConnectionStatus


class TestMessageRequest(BaseModel):
    to: str = Field(min_length=5, max_length=32)


class TestMessageResponse(BaseModel):
    success: bool
    detail: str


# ── GET /config — public identifiers only, safe to send to the frontend ────

@router.get("/config", response_model=WhatsAppConnectConfig)
async def get_connect_config(
    user: AuthUser = Depends(require_owner_or_manager),
):
    if not settings.META_APP_ID or not settings.META_ES_CONFIG_ID:
        return WhatsAppConnectConfig(
            available=False,
            reason="WhatsApp Embedded Signup is not configured on this platform yet "
                   "(META_APP_ID/META_ES_CONFIG_ID unset). Contact NextGen support.",
        )
    return WhatsAppConnectConfig(
        available=True,
        app_id=settings.META_APP_ID,
        config_id=settings.META_ES_CONFIG_ID,
        graph_version=settings.META_API_VERSION,
    )


# ── GET / — status for the UI ───────────────────────────────────────────────

@router.get("", response_model=WhatsAppConnectionStatus)
async def get_connection_status(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    business = await _require_whatsapp_module_enabled(db, user.business_id)
    connection = await connection_service.get_connection(db, business.id)
    return _status_response(business, connection)


# ── POST /exchange — run the Embedded Signup connect flow ──────────────────

@router.post("/exchange", response_model=ExchangeResponse)
@limiter.limit(f"{settings.WHATSAPP_CONNECT_RATE_LIMIT_PER_MINUTE}/minute")
async def exchange_code(
    body: ExchangeRequest,
    request: Request,
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    business = await _require_whatsapp_module_enabled(db, user.business_id)

    outcome = await connection_service.run_exchange(
        db, business,
        code=body.code,
        waba_id=body.waba_id,
        phone_number_id=body.phone_number_id,
        connected_by_user_id=user.user_id,
    )

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=business.id,
        actor_user_id=user.user_id,
        action="whatsapp.connection.connected" if outcome.success else "whatsapp.connection.connect_failed",
        target_type="whatsapp_connection",
        target_id=outcome.connection.id,
        diff_json={
            "success": outcome.success,
            "failed_step": outcome.failed_step,
            "waba_id": body.waba_id,
            "phone_number_id": body.phone_number_id,
        },
    ))
    await db.commit()

    response = ExchangeResponse(
        success=outcome.success,
        failed_step=outcome.failed_step,
        message=outcome.message,
        connection=_status_response(business, outcome.connection),
    )

    if not outcome.success and outcome.failed_step == "claim_routing_number":
        raise AppError(
            "WHATSAPP_NUMBER_ALREADY_CONNECTED", outcome.message, 409,
            details={"failed_step": outcome.failed_step},
        )
    return response


# ── POST /health-check ──────────────────────────────────────────────────────

@router.post("/health-check", response_model=HealthCheckResponse)
async def run_health_check(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    business = await _require_whatsapp_module_enabled(db, user.business_id)
    try:
        connection, result = await connection_service.health_check(db, business)
    except connection_service.ConnectionNotFound:
        raise AppError("WHATSAPP_NOT_CONNECTED", "This business has no WhatsApp connection to check.", 409)

    if not result["ok"]:
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=business.id,
            actor_user_id=user.user_id,
            action="whatsapp.connection.health_check_failed",
            target_type="whatsapp_connection",
            target_id=connection.id,
            diff_json={"detail": result["detail"]},
        ))
        await db.commit()

    return HealthCheckResponse(
        ok=result["ok"],
        detail=result["detail"],
        technical_details=result["technical_details"],
        connection=_status_response(business, connection),
    )


# ── POST /test-message ──────────────────────────────────────────────────────

@router.post("/test-message", response_model=TestMessageResponse)
@limiter.limit(f"{settings.WHATSAPP_TEST_MESSAGE_RATE_LIMIT_PER_MINUTE}/minute")
async def send_test_message(
    body: TestMessageRequest,
    request: Request,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    business = await _require_whatsapp_module_enabled(db, user.business_id)
    connection = await connection_service.get_connection(db, business.id)
    if connection is None or connection.status != "CONNECTED":
        raise AppError(
            "WHATSAPP_NOT_CONNECTED",
            "This business has no active WhatsApp connection to send a test message from.",
            409,
        )

    try:
        token = decrypt_credential(connection.access_token_encrypted)
    except CredentialDecryptionError:
        raise AppError("WHATSAPP_CREDENTIAL_ERROR", "Stored WhatsApp credentials could not be read.", 500)
    if not token:
        raise AppError("WHATSAPP_NOT_CONNECTED", "No stored WhatsApp token for this business.", 409)

    client = MetaGraphClient()
    payload = {
        "recipient_type": "individual",
        "to": body.to,
        "type": "text",
        "text": {"body": "This is a test message from your NextGen WhatsApp connection."},
    }
    try:
        await client.send_message(connection.phone_number_id, token, payload)
    except GraphAPIError as exc:
        if exc.is_invalid_token:
            await connection_service.mark_invalid_token(
                db, connection,
                "Meta reports this connection's token is no longer valid (error 190) — reconnect required.",
            )
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=business.id,
            actor_user_id=user.user_id,
            action="whatsapp.connection.test_message_failed",
            target_type="whatsapp_connection",
            target_id=connection.id,
            diff_json={"error": str(exc), "error_code": exc.error_code},
        ))
        await db.commit()
        return TestMessageResponse(success=False, detail=f"Send failed: {exc}")

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=business.id,
        actor_user_id=user.user_id,
        action="whatsapp.connection.test_message_sent",
        target_type="whatsapp_connection",
        target_id=connection.id,
        diff_json={"to_masked": body.to[:4] + "***" if len(body.to) > 4 else "***"},
    ))
    await db.commit()
    return TestMessageResponse(success=True, detail="Test message sent.")


# ── DELETE / — disconnect ───────────────────────────────────────────────────

@router.delete("", response_model=WhatsAppConnectionStatus)
async def disconnect_whatsapp(
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    business = await _require_whatsapp_module_enabled(db, user.business_id)
    try:
        connection = await connection_service.disconnect(db, business)
    except connection_service.ConnectionNotFound:
        raise AppError("WHATSAPP_NOT_CONNECTED", "This business has no WhatsApp connection to disconnect.", 409)

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=business.id,
        actor_user_id=user.user_id,
        action="whatsapp.connection.disconnected",
        target_type="whatsapp_connection",
        target_id=connection.id,
        diff_json={"phone_number_id": connection.phone_number_id},
    ))
    await db.commit()

    return _status_response(business, connection)
