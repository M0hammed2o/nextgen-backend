"""
Business-plane routes for the delivery marketplace module (Uber Eats today).

    GET  /business/delivery/connections                  list this tenant's connections
    GET  /business/delivery/connections/{provider}        one connection (synthesised default if none)
    POST /business/delivery/connections/{provider}/disable   pause/switch off a live connection
    POST /business/delivery/connections/{provider}/enable    request re-enable (never -> CONNECTED)
    GET  /business/delivery/orders                        list delivery orders, filterable by status
    GET  /business/delivery/orders/{order_id}              one delivery order
    POST /business/delivery/orders/{order_id}/accept       manual accept (NEEDS_MAPPING only)
    POST /business/delivery/orders/{order_id}/deny         manual deny (any open status)
    GET  /business/delivery/menu-mappings                  list provider items + mapping status
    POST /business/delivery/menu-mappings                  create/update a mapping

Studied `routes_external_orders.py` (closest analogue for the mapping
domain) and `routes_whatsapp_connect.py` (closest analogue for
connection-lifecycle endpoints) and follows their established shapes —
`_require_delivery_module_enabled` mirrors `_require_whatsapp_module_enabled`,
and the mapping upsert endpoint reuses `normalise_key` from
`external_orders/service.py` rather than re-implementing key normalisation.

NEVER RETURNS A CREDENTIAL, TOKEN, OR ENCRYPTED FIELD
----------------------------------------------------------
`DeliveryConnectionResponse` deliberately omits `provisioning_token_encrypted`
— same discipline as `WhatsAppConnectionStatus` in `routes_whatsapp_connect.py`,
which deliberately omits `access_token_encrypted`.

`business_id` IS SOURCED FROM THE JWT ONLY
-----------------------------------------------
Every query below filters on `user.business_id` (from `AuthUser`, set by
`require_owner_or_manager`/`require_staff_or_above`) — never from a path,
query, or body parameter. This is the platform's core tenant-isolation
invariant (see SECURITY.md).

A CONNECTION CAN NEVER BE SELF-PROMOTED TO CONNECTED FROM THIS ROUTER
---------------------------------------------------------------------------
`status == "CONNECTED"` means "Uber has actually approved and activated this
store" (see `shared/models/delivery.py`). No endpoint here ever writes that
value. `disable` only ever moves a connection TOWARDS `DISABLED` (a
downgrade); `enable` only ever moves a `DISABLED` connection to
`ATTENTION_REQUIRED` (never straight back to `CONNECTED`), so a human/support
review — or a future automated Uber status re-sync (Phase 2 provisioning
flow) — decides whether it is genuinely still live, not a business-plane
API call. This is a deliberate design decision, not an oversight; see this
module's own docstring above each endpoint for the exact reasoning.

WHAT "MANUAL ACCEPT" ACTUALLY DOES, GIVEN THE SHIPPED WORKER PLANE
------------------------------------------------------------------------
`order_service.py::_handle_order` auto-accepts any delivery order whose
lines fully resolve against `external_product_mappings` — there is no
`auto_accept_enabled` column/setting anywhere in this codebase, and no
staff gate exists for an already-resolved order (it goes straight from
FETCHING to ACCEPTED). The only state where a human decision is actually
pending today is `NEEDS_MAPPING` (unresolved items). "Accept" here therefore
means: reset the row to `RECEIVED` and re-enqueue the SAME
`process_delivery_order` ARQ job the webhook plane uses — reusing the exact
worker pipeline rather than re-implementing fetch/resolve/create-order/
accept logic in this route. If mappings still don't resolve, the order goes
back to `NEEDS_MAPPING` exactly as it would on first pass. This is flagged
as an implementation decision in this task's final report, not a literal
`auto_accept_enabled` toggle (building one would be a worker-plane/schema
change beyond this task's two declared gaps).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.delivery import order_service
from shared.enums import AuditScope, ExternalMappingKind, ExternalPlatform
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem

logger = logging.getLogger("nextgen.delivery.routes")

router = APIRouter(prefix="/business/delivery", tags=["delivery"])

_PROVIDER_PATTERN = "^(UBER_EATS|MR_D)$"
_KIND_PATTERN = "^(ITEM|MODIFIER|MODIFIER_GROUP|CATEGORY)$"
_DENY_REASON_CODES = (
    "STORE_CLOSED", "POS_NOT_READY", "POS_OFFLINE", "ITEM_AVAILABILITY",
    "MISSING_ITEM", "MISSING_INFO", "PRICING", "CAPACITY", "ADDRESS",
    "SPECIAL_INSTRUCTIONS", "OTHER",
)
_DENY_REASON_PATTERN = "^(" + "|".join(_DENY_REASON_CODES) + ")$"

PROVIDER_LABELS = {
    ExternalPlatform.UBER_EATS.value: "Uber Eats",
    ExternalPlatform.MR_D.value: "Mr D",
}

_OPEN_ORDER_STATUSES = ("RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT")


# ── Guard (mirrors _require_whatsapp_module_enabled in routes_whatsapp_connect.py) ──

async def _require_delivery_module_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.delivery_platforms_enabled:
        raise AppError(
            "DELIVERY_NOT_ENABLED",
            "The delivery marketplace module is not enabled for this business.",
            409,
        )
    return business


# ── Schemas ──────────────────────────────────────────────────────────────────

class DeliveryConnectionResponse(BaseModel):
    """
    Never includes provisioning_token_encrypted or any decrypted secret —
    the frontend never needs them (mirrors WhatsAppConnectionStatus).
    """
    id: uuid.UUID | None
    provider: str
    provider_label: str
    status: str
    environment: str
    external_store_id: str | None
    store_status: str | None
    last_status_synced_at: datetime | None
    activated_at: datetime | None
    disconnected_at: datetime | None
    error_reason: str | None
    needs_attention: bool
    last_webhook_at: datetime | None
    last_order_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


def _synthesised_connection(provider: str) -> DeliveryConnectionResponse:
    return DeliveryConnectionResponse(
        id=None,
        provider=provider,
        provider_label=PROVIDER_LABELS.get(provider, provider),
        status="NOT_CONNECTED",
        environment="production",
        external_store_id=None,
        store_status=None,
        last_status_synced_at=None,
        activated_at=None,
        disconnected_at=None,
        error_reason=None,
        needs_attention=False,
        last_webhook_at=None,
        last_order_at=None,
        created_at=None,
        updated_at=None,
    )


async def _connection_response(
    db: AsyncSession, business_id: uuid.UUID, connection: DeliveryConnection,
) -> DeliveryConnectionResponse:
    last_webhook_at = (
        await db.execute(
            select(func.max(DeliveryWebhookEvent.received_at)).where(
                DeliveryWebhookEvent.business_id == business_id,
                DeliveryWebhookEvent.provider == connection.provider,
            )
        )
    ).scalar_one_or_none()
    last_order_at = (
        await db.execute(
            select(func.max(DeliveryOrder.created_at)).where(
                DeliveryOrder.business_id == business_id,
                DeliveryOrder.provider == connection.provider,
            )
        )
    ).scalar_one_or_none()
    return DeliveryConnectionResponse(
        id=connection.id,
        provider=connection.provider,
        provider_label=PROVIDER_LABELS.get(connection.provider, connection.provider),
        status=connection.status,
        environment=connection.environment,
        external_store_id=connection.external_store_id,
        store_status=connection.store_status,
        last_status_synced_at=connection.last_status_synced_at,
        activated_at=connection.activated_at,
        disconnected_at=connection.disconnected_at,
        error_reason=connection.error_reason,
        needs_attention=connection.status == "ATTENTION_REQUIRED",
        last_webhook_at=last_webhook_at,
        last_order_at=last_order_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


class DeliveryOrderResponse(BaseModel):
    id: uuid.UUID
    provider: str
    provider_label: str
    external_order_id: str
    status: str
    order_id: uuid.UUID | None
    gross_cents: int | None
    fee_cents: int | None
    currency: str
    placed_at: datetime | None
    accept_deadline_at: datetime | None
    accepted_at: datetime | None
    denied_at: datetime | None
    cancelled_at: datetime | None
    deny_reason_code: str | None
    deny_reason_text: str | None
    unresolved_items: list | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: DeliveryOrder) -> "DeliveryOrderResponse":
        return cls(
            id=row.id,
            provider=row.provider,
            provider_label=PROVIDER_LABELS.get(row.provider, row.provider),
            external_order_id=row.external_order_id,
            status=row.status,
            order_id=row.order_id,
            gross_cents=row.gross_cents,
            fee_cents=row.fee_cents,
            currency=row.currency,
            placed_at=row.placed_at,
            accept_deadline_at=row.accept_deadline_at,
            accepted_at=row.accepted_at,
            denied_at=row.denied_at,
            cancelled_at=row.cancelled_at,
            deny_reason_code=row.deny_reason_code,
            deny_reason_text=row.deny_reason_text,
            unresolved_items=row.unresolved_items_json,
            failure_reason=row.failure_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class DeliveryOrderListResponse(BaseModel):
    orders: list[DeliveryOrderResponse]
    total: int
    limit: int
    offset: int


class DenyOrderRequest(BaseModel):
    reason_code: str = Field(pattern=_DENY_REASON_PATTERN)
    notes: str | None = Field(default=None, max_length=1000)


class MappingResponse(BaseModel):
    id: uuid.UUID
    platform: str
    kind: str
    external_id: str | None
    external_text: str
    status: str
    status_reason: str | None
    menu_item_id: uuid.UUID | None
    menu_item_name: str | None
    option_id: str | None
    menu_add_on_id: uuid.UUID | None
    menu_add_on_name: str | None
    removable_ref_id: str | None
    is_ignored: bool
    last_seen_at: datetime | None
    times_used: int


class MappingUpsertRequest(BaseModel):
    platform: str = Field(pattern=_PROVIDER_PATTERN)
    kind: str = Field(default="ITEM", pattern=_KIND_PATTERN)
    # THE live-connection identity key (migration 0043's partial unique
    # index). Required here — this endpoint exists specifically for live
    # API connections, unlike routes_external_orders.py's text-only upsert.
    external_id: str = Field(min_length=1, max_length=128)
    external_text: str = Field(min_length=1, max_length=500)
    menu_item_id: uuid.UUID | None = None
    option_id: str | None = Field(default=None, max_length=64)
    menu_add_on_id: uuid.UUID | None = None
    removable_ref_id: str | None = Field(default=None, max_length=64)
    is_ignored: bool = False


def _mapping_response(m: ExternalProductMapping, item_names: dict, add_on_names: dict) -> MappingResponse:
    return MappingResponse(
        id=m.id,
        platform=m.platform,
        kind=m.kind,
        external_id=m.external_id,
        external_text=m.external_text,
        status=m.status,
        status_reason=m.status_reason,
        menu_item_id=m.menu_item_id,
        menu_item_name=item_names.get(m.menu_item_id),
        option_id=m.option_id,
        menu_add_on_id=m.menu_add_on_id,
        menu_add_on_name=add_on_names.get(m.menu_add_on_id),
        removable_ref_id=m.removable_ref_id,
        is_ignored=m.is_ignored,
        last_seen_at=m.last_seen_at,
        times_used=m.times_used,
    )


# ── Connections ──────────────────────────────────────────────────────────────

@router.get("/connections", response_model=list[DeliveryConnectionResponse])
async def list_connections(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_delivery_module_enabled(db, user.business_id)
    rows = (
        await db.execute(
            select(DeliveryConnection).where(DeliveryConnection.business_id == user.business_id)
        )
    ).scalars().all()
    return [await _connection_response(db, user.business_id, r) for r in rows]


@router.get("/connections/{provider}", response_model=DeliveryConnectionResponse)
async def get_connection(
    provider: str,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    if provider not in PROVIDER_LABELS:
        raise AppError("UNKNOWN_PROVIDER", f"Unknown provider {provider!r}", 422)
    await _require_delivery_module_enabled(db, user.business_id)
    connection = (
        await db.execute(
            select(DeliveryConnection).where(
                DeliveryConnection.business_id == user.business_id,
                DeliveryConnection.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return _synthesised_connection(provider)
    return await _connection_response(db, user.business_id, connection)


@router.post("/connections/{provider}/disable", response_model=DeliveryConnectionResponse)
async def disable_connection(
    provider: str,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Pause/switch off this tenant's own connection. Always a downgrade — never
    touches CONNECTED in the forward direction. Valid from any status except
    the two that already mean "not live" (NOT_CONNECTED/DISABLED).
    """
    await _require_delivery_module_enabled(db, user.business_id)
    connection = (
        await db.execute(
            select(DeliveryConnection).where(
                DeliveryConnection.business_id == user.business_id,
                DeliveryConnection.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise NotFoundError("Delivery connection", provider)
    if connection.status in ("NOT_CONNECTED", "DISABLED"):
        raise AppError(
            "DELIVERY_CONNECTION_ALREADY_INACTIVE",
            f"This connection is already {connection.status}.",
            409,
        )

    previous_status = connection.status
    connection.status = "DISABLED"
    connection.disconnected_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.connection.disabled", target_type="delivery_connection",
        target_id=connection.id,
        diff_json={"provider": provider, "previous_status": previous_status},
    ))
    await db.commit()
    return await _connection_response(db, user.business_id, connection)


@router.post("/connections/{provider}/enable", response_model=DeliveryConnectionResponse)
async def enable_connection(
    provider: str,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Request re-enabling a DISABLED connection. Deliberately never writes
    CONNECTED — see module docstring. Moves to ATTENTION_REQUIRED so a
    human/support review (or a future automated Uber status re-sync) decides
    whether it is genuinely still live before any order can flow through it.
    """
    await _require_delivery_module_enabled(db, user.business_id)
    connection = (
        await db.execute(
            select(DeliveryConnection).where(
                DeliveryConnection.business_id == user.business_id,
                DeliveryConnection.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise NotFoundError("Delivery connection", provider)
    if connection.status != "DISABLED":
        raise AppError(
            "DELIVERY_CONNECTION_NOT_DISABLED",
            "Only a disabled connection can be re-enabled from here.",
            409,
        )

    connection.status = "ATTENTION_REQUIRED"
    connection.disconnected_at = None
    connection.error_reason = (
        "Re-enabled by business — awaiting confirmation this connection is "
        "still live and approved before it can process real orders."
    )
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.connection.enable_requested", target_type="delivery_connection",
        target_id=connection.id,
        diff_json={"provider": provider},
    ))
    await db.commit()
    return await _connection_response(db, user.business_id, connection)


# ── Orders ───────────────────────────────────────────────────────────────────

async def _load_order_or_404(
    db: AsyncSession, *, business_id: uuid.UUID, order_id: uuid.UUID, for_update: bool = False,
) -> DeliveryOrder:
    stmt = select(DeliveryOrder).where(
        DeliveryOrder.id == order_id,
        DeliveryOrder.business_id == business_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Delivery order", str(order_id))
    return row


@router.get("/orders", response_model=DeliveryOrderListResponse)
async def list_orders(
    status: str | None = Query(default=None, description="Comma-separated status filter"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_delivery_module_enabled(db, user.business_id)

    stmt = select(DeliveryOrder).where(DeliveryOrder.business_id == user.business_id)
    count_stmt = select(func.count()).select_from(DeliveryOrder).where(
        DeliveryOrder.business_id == user.business_id
    )
    if status:
        statuses = [s.strip().upper() for s in status.split(",") if s.strip()]
        stmt = stmt.where(DeliveryOrder.status.in_(statuses))
        count_stmt = count_stmt.where(DeliveryOrder.status.in_(statuses))

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(DeliveryOrder.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return DeliveryOrderListResponse(
        orders=[DeliveryOrderResponse.from_row(r) for r in rows],
        total=total, limit=limit, offset=offset,
    )


@router.get("/orders/{order_id}", response_model=DeliveryOrderResponse)
async def get_order(
    order_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(db, business_id=user.business_id, order_id=order_id)
    return DeliveryOrderResponse.from_row(row)


@router.post("/orders/{order_id}/accept", response_model=DeliveryOrderResponse)
async def accept_order(
    order_id: uuid.UUID,
    request: Request,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    See module docstring: only meaningful for NEEDS_MAPPING today. Resets
    the row to RECEIVED and re-enqueues the SAME process_delivery_order ARQ
    job the webhook plane uses — no parallel accept logic in this route.
    """
    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(
        db, business_id=user.business_id, order_id=order_id, for_update=True
    )
    if row.status != "NEEDS_MAPPING":
        raise AppError(
            "DELIVERY_ORDER_NOT_ACCEPTABLE",
            f"This order is {row.status}, not NEEDS_MAPPING — nothing to manually accept.",
            409,
        )

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise AppError(
            "DELIVERY_WORKER_UNAVAILABLE",
            "Could not queue this order for processing right now — please try again shortly.",
            503,
        )

    row.status = "RECEIVED"
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.order.manual_accept_requested", target_type="delivery_order",
        target_id=row.id,
        diff_json={"provider": row.provider, "external_order_id": row.external_order_id},
    ))
    await db.commit()

    try:
        await arq_pool.enqueue_job(
            "process_delivery_order", row.provider, str(row.business_id),
            row.external_order_id, None,
        )
    except Exception:
        logger.exception(
            "DELIVERY_MANUAL_ACCEPT_ENQUEUE_FAILED: business=%s order_id=%s",
            user.business_id, order_id,
        )
        raise AppError(
            "DELIVERY_WORKER_UNAVAILABLE",
            "Could not queue this order for processing right now — please try again shortly.",
            503,
        )

    await db.refresh(row)
    return DeliveryOrderResponse.from_row(row)


@router.post("/orders/{order_id}/deny", response_model=DeliveryOrderResponse)
async def deny_order(
    order_id: uuid.UUID,
    body: DenyOrderRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Manual deny, reusing order_service._issue_deny (the exact same helper
    the accept-deadline watchdog uses) rather than re-implementing the
    adapter deny call here.
    """
    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(
        db, business_id=user.business_id, order_id=order_id, for_update=True
    )
    if row.status not in _OPEN_ORDER_STATUSES:
        raise AppError(
            "DELIVERY_ORDER_NOT_DENIABLE",
            f"This order is {row.status} — it is no longer open to deny.",
            409,
        )

    connection = (
        await db.get(DeliveryConnection, row.delivery_connection_id)
        if row.delivery_connection_id else None
    )
    await order_service._issue_deny(
        db, row=row, connection=connection,
        reason_code=body.reason_code,
        explanation=body.notes or "Denied by staff via the NextGen dashboard.",
    )
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.order.manual_deny", target_type="delivery_order",
        target_id=row.id,
        diff_json={
            "provider": row.provider, "external_order_id": row.external_order_id,
            "reason_code": body.reason_code,
        },
    ))
    await db.commit()
    await db.refresh(row)
    return DeliveryOrderResponse.from_row(row)


# ── Menu mappings ────────────────────────────────────────────────────────────

@router.get("/menu-mappings", response_model=list[MappingResponse])
async def list_menu_mappings(
    provider: str | None = Query(default=None, pattern=_PROVIDER_PATTERN),
    status: str | None = Query(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_delivery_module_enabled(db, user.business_id)

    stmt = select(ExternalProductMapping).where(
        ExternalProductMapping.business_id == user.business_id,
        ExternalProductMapping.external_id.isnot(None),
    )
    if provider:
        stmt = stmt.where(ExternalProductMapping.platform == provider)
    if status:
        stmt = stmt.where(ExternalProductMapping.status == status.upper())
    rows = (await db.execute(stmt.order_by(ExternalProductMapping.external_text))).scalars().all()

    item_names = {
        m.id: m.name
        for m in (
            await db.execute(select(MenuItem).where(MenuItem.business_id == user.business_id))
        ).scalars().all()
    }
    add_on_names = {
        a.id: a.name
        for a in (
            await db.execute(select(MenuAddOn).where(MenuAddOn.business_id == user.business_id))
        ).scalars().all()
    }
    return [_mapping_response(m, item_names, add_on_names) for m in rows]


@router.post("/menu-mappings", status_code=201, response_model=MappingResponse)
async def upsert_menu_mapping(
    body: MappingUpsertRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Create/update a mapping pointing a provider item/modifier at this
    tenant's own menu item/option/add-on. Reuses normalise_key() from the
    CSV importer's service module rather than re-implementing key
    normalisation — the CSV importer's own lookup path is untouched (it
    never reads external_id).
    """
    from backend.app.external_orders.service import normalise_key

    await _require_delivery_module_enabled(db, user.business_id)

    if not body.is_ignored:
        if body.kind == ExternalMappingKind.ITEM.value and not body.menu_item_id:
            raise AppError(
                "MAPPING_TARGET_REQUIRED",
                "Choose the menu item this product is, or mark it as not affecting stock",
                422,
            )
        if body.kind == ExternalMappingKind.MODIFIER.value and not (
            body.menu_add_on_id or body.removable_ref_id or body.option_id
        ):
            raise AppError(
                "MAPPING_TARGET_REQUIRED",
                "Choose what this modifier means, or mark it as not affecting stock",
                422,
            )

    # Targets must belong to THIS tenant — never trust an id alone.
    if body.menu_item_id:
        exists = (
            await db.execute(
                select(MenuItem.id).where(
                    MenuItem.id == body.menu_item_id, MenuItem.business_id == user.business_id,
                )
            )
        ).first()
        if not exists:
            raise NotFoundError("Menu item", str(body.menu_item_id))
    if body.menu_add_on_id:
        exists = (
            await db.execute(
                select(MenuAddOn.id).where(
                    MenuAddOn.id == body.menu_add_on_id, MenuAddOn.business_id == user.business_id,
                )
            )
        ).first()
        if not exists:
            raise NotFoundError("Add-on", str(body.menu_add_on_id))

    existing = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.business_id == user.business_id,
                ExternalProductMapping.platform == body.platform,
                ExternalProductMapping.kind == body.kind,
                ExternalProductMapping.external_id == body.external_id,
            )
        )
    ).scalar_one_or_none()

    status_value = "IGNORED" if body.is_ignored else "MAPPED"
    now = datetime.now(timezone.utc)

    if existing:
        existing.external_text = body.external_text
        existing.external_key = normalise_key(body.external_text)
        existing.menu_item_id = body.menu_item_id
        existing.option_id = body.option_id
        existing.menu_add_on_id = body.menu_add_on_id
        existing.removable_ref_id = body.removable_ref_id
        existing.is_ignored = body.is_ignored
        existing.status = status_value
        existing.status_reason = None
        existing.last_seen_at = now
        mapping = existing
    else:
        mapping = ExternalProductMapping(
            business_id=user.business_id,
            platform=body.platform,
            kind=body.kind,
            external_text=body.external_text,
            external_key=normalise_key(body.external_text),
            external_id=body.external_id,
            menu_item_id=body.menu_item_id,
            option_id=body.option_id,
            menu_add_on_id=body.menu_add_on_id,
            removable_ref_id=body.removable_ref_id,
            is_ignored=body.is_ignored,
            status=status_value,
            last_seen_at=now,
            created_by_user_id=user.user_id,
        )
        db.add(mapping)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError("mapping", "external id / normalised text") from exc
    await db.refresh(mapping)

    item_names = {
        m.id: m.name
        for m in (
            await db.execute(select(MenuItem).where(MenuItem.business_id == user.business_id))
        ).scalars().all()
    }
    add_on_names = {
        a.id: a.name
        for a in (
            await db.execute(select(MenuAddOn).where(MenuAddOn.business_id == user.business_id))
        ).scalars().all()
    }
    return _mapping_response(mapping, item_names, add_on_names)
