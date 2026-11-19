"""
Admin delivery-connection management — SUPER_ADMIN only.

    GET /admin/businesses/{business_id}/delivery-connections
    PUT /admin/businesses/{business_id}/delivery-connections/{provider}

WHY THIS EXISTS
------------------
A `delivery_connections` row (and its `external_store_id`, THE tenant key for
inbound webhooks) could previously only be created by direct database
action. Uber's own onboarding is operator-driven anyway: sandbox test
stores are issued by Uber's Integration Tech Support ("Request test stores
from Integration Tech Support" — developer.uber.com/docs/eats/guides/sandbox),
and production needs Uber's written approval. This is the audited,
validated operator path for both, instead of hand-written SQL.

THE RULES IT ENFORCES (and the business plane can never bypass)
--------------------------------------------------------------------
* `SANDBOX` only on the sandbox environment; `AWAITING_APPROVAL` and
  `CONNECTED` only on production; each of those three needs a store id.
* `CONNECTED` — the ONLY status any UI may call live — additionally needs
  an `uber_approval_reference` (Uber's written approval: email/ticket ref),
  the business's delivery entitlements switched on, and production app
  credentials configured on this deployment. All recorded in the audit row.
* Mr D can only ever be NOT_CONNECTED or DISABLED here: there is no live Mr
  D integration (no partner API — docs/MRD_API_ACCESS_2026-10.md), so live
  connectivity stays "Pending Partner Approval" and Mr D is reconciled from
  its CSV export.
* A store id can belong to one business only — enforced by the database's
  partial unique index; a clash is a 409, never a silent re-point.

Business-plane owners/managers still can't promote a connection (see
backend/app/api/v1/routes_delivery.py — "never self-promote to CONNECTED").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.app.core.deps import require_super_admin
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import get_db
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection

logger = logging.getLogger("nextgen.admin.delivery")
router = APIRouter(prefix="/admin/businesses", tags=["admin-delivery-connections"])

_PROVIDER_PATTERN = "^(UBER_EATS|MR_D)$"
_STATUS_PATTERN = "^(NOT_CONNECTED|SANDBOX|AWAITING_APPROVAL|CONNECTED|ATTENTION_REQUIRED|DISABLED)$"
_MRD_ALLOWED_STATUSES = ("NOT_CONNECTED", "DISABLED")


class AdminDeliveryConnectionResponse(BaseModel):
    """Never includes provisioning_token_encrypted or any other credential."""
    id: uuid.UUID
    business_id: uuid.UUID
    provider: str
    status: str
    environment: str
    external_store_id: str | None
    auto_accept_enabled: bool
    store_status: str | None
    error_reason: str | None
    activated_at: datetime | None
    disconnected_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, c: DeliveryConnection) -> "AdminDeliveryConnectionResponse":
        return cls(
            id=c.id, business_id=c.business_id, provider=c.provider, status=c.status,
            environment=c.environment, external_store_id=c.external_store_id,
            auto_accept_enabled=bool(c.auto_accept_enabled), store_status=c.store_status,
            error_reason=c.error_reason, activated_at=c.activated_at,
            disconnected_at=c.disconnected_at, updated_at=c.updated_at,
        )


class AdminDeliveryConnectionUpsert(BaseModel):
    status: str = Field(pattern=_STATUS_PATTERN)
    environment: str = Field(pattern="^(production|sandbox)$")
    # Uber store ids are UUID-shaped; ASCII letters, digits and hyphens only
    # (this value is later interpolated into Uber API paths).
    external_store_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,128}$")
    uber_approval_reference: str | None = Field(default=None, min_length=3, max_length=200)
    notes: str | None = Field(default=None, max_length=500)


def _validate(body: AdminDeliveryConnectionUpsert, provider: str, business: Business) -> None:
    if provider == "MR_D" and body.status not in _MRD_ALLOWED_STATUSES:
        raise AppError(
            "MRD_PENDING_PARTNER_APPROVAL",
            "Mr D live connectivity is Pending Partner Approval — no live Mr D integration "
            "exists. Mr D orders are reconciled from Mr D's CSV export.",
            409,
        )
    if body.status == "SANDBOX" and body.environment != "sandbox":
        raise AppError("DELIVERY_STATUS_ENV_MISMATCH", "SANDBOX requires environment=sandbox.", 422)
    if body.status in ("AWAITING_APPROVAL", "CONNECTED") and body.environment != "production":
        raise AppError(
            "DELIVERY_STATUS_ENV_MISMATCH", f"{body.status} requires environment=production.", 422,
        )
    if body.status in ("SANDBOX", "AWAITING_APPROVAL", "CONNECTED") and not body.external_store_id:
        raise AppError("DELIVERY_STORE_ID_REQUIRED", f"{body.status} requires the provider's store id.", 422)
    if body.status == "CONNECTED":
        if not body.uber_approval_reference:
            raise AppError(
                "DELIVERY_APPROVAL_REFERENCE_REQUIRED",
                "CONNECTED means Uber has approved this store for real orders — record "
                "Uber's written approval reference (email/ticket) to set it.",
                422,
            )
        if not (business.delivery_platforms_enabled and business.uber_eats_enabled):
            raise AppError(
                "DELIVERY_ENTITLEMENT_OFF",
                "Turn on delivery_platforms_enabled and uber_eats_enabled for this business "
                "first — otherwise every order would be denied.",
                409,
            )
        from backend.app.core.config import get_settings
        from backend.app.delivery.uber.environments import get_environment_config

        if get_settings().UBER_EATS_SANDBOX_ONLY_DEPLOYMENT or not get_environment_config("production").has_credentials:
            raise AppError(
                "DELIVERY_PRODUCTION_CREDENTIALS_MISSING",
                "This deployment has no production Uber Eats app credentials configured.",
                409,
            )


@router.get(
    "/{business_id}/delivery-connections",
    response_model=list[AdminDeliveryConnectionResponse],
)
async def list_delivery_connections(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    business = await db.get(Business, business_id)
    if business is None:
        raise NotFoundError("Business", str(business_id))
    rows = (
        await db.execute(
            select(DeliveryConnection).where(DeliveryConnection.business_id == business_id)
        )
    ).scalars().all()
    return [AdminDeliveryConnectionResponse.from_row(r) for r in rows]


@router.put(
    "/{business_id}/delivery-connections/{provider}",
    response_model=AdminDeliveryConnectionResponse,
)
async def upsert_delivery_connection(
    business_id: uuid.UUID,
    provider: str,
    body: AdminDeliveryConnectionUpsert,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    if provider not in ("UBER_EATS", "MR_D"):
        raise AppError("UNKNOWN_PROVIDER", f"Unknown provider {provider!r}", 422)
    business = await db.get(Business, business_id)
    if business is None:
        raise NotFoundError("Business", str(business_id))
    _validate(body, provider, business)

    connection = (
        await db.execute(
            select(DeliveryConnection).where(
                DeliveryConnection.business_id == business_id,
                DeliveryConnection.provider == provider,
            )
        )
    ).scalar_one_or_none()
    previous = None
    now = datetime.now(timezone.utc)
    if connection is None:
        connection = DeliveryConnection(
            business_id=business_id, provider=provider, created_by_user_id=user.user_id,
            auto_accept_enabled=False,
        )
        db.add(connection)
    else:
        previous = {
            "status": connection.status, "environment": connection.environment,
            "external_store_id": connection.external_store_id,
        }

    connection.status = body.status
    connection.environment = body.environment
    connection.external_store_id = body.external_store_id
    if body.status in ("SANDBOX", "AWAITING_APPROVAL", "CONNECTED"):
        connection.error_reason = None
        connection.disconnected_at = None
        if body.status == "CONNECTED" and connection.activated_at is None:
            connection.activated_at = now
    elif body.status == "DISABLED":
        connection.disconnected_at = now

    try:
        # Flush first: gives a new row its id for the audit entry, and
        # surfaces a store-id clash (partial unique index) as a clean 409.
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(
            "DELIVERY_STORE_ID_IN_USE",
            "That store id is already connected to another business — one store, one tenant.",
            409,
        ) from exc

    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=business_id,
        actor_user_id=user.user_id,
        action="admin.delivery_connection.upserted",
        target_type="delivery_connection",
        target_id=connection.id,
        diff_json={
            "provider": provider,
            "previous": previous,
            "new": {
                "status": body.status, "environment": body.environment,
                "external_store_id": body.external_store_id,
            },
            "uber_approval_reference": body.uber_approval_reference,
            "notes": body.notes,
        },
    ))
    await db.commit()
    await db.refresh(connection)
    logger.warning(
        "ADMIN_DELIVERY_CONNECTION_UPSERTED: business=%s provider=%s status=%s env=%s by=%s",
        business_id, provider, body.status, body.environment, user.user_id,
    )
    return AdminDeliveryConnectionResponse.from_row(connection)
