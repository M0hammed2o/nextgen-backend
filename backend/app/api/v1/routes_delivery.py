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

Added 2026-10-26 (marketplace completion pass):

    GET  /business/delivery/queue                          STAFF view of orders still waiting on someone
    POST /business/delivery/orders/{order_id}/retry-accept  re-send an unanswered accept (staff+)
    POST /business/delivery/orders/{order_id}/mark-accepted manager confirms the provider accepted
    GET  /business/delivery/connections/{provider}/store-status   read live/paused from the provider
    POST /business/delivery/connections/{provider}/store-status   pause/resume on the provider
    POST /business/delivery/menu-mappings/import           read the provider menu into mappings
    GET  /business/delivery/health                         per-provider connection/order-flow health

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

WHAT "MANUAL ACCEPT" ACTUALLY DOES (migration 0046, `auto_accept_enabled`)
------------------------------------------------------------------------
`order_service.py::_handle_order` now gates on the connection's
`auto_accept_enabled` (default FALSE): a fully-resolved order stops at
`READY_TO_ACCEPT` instead of auto-committing to the kitchen. "Accept" here
means one of two things depending on the row's CURRENT status, but always by
resetting the row to `RECEIVED` and re-enqueuing the SAME
`process_delivery_order` ARQ job the webhook plane uses — reusing the exact
worker pipeline rather than re-implementing fetch/resolve/create-order/
accept logic in this route:
  - `NEEDS_MAPPING` — staff just fixed a mapping; re-attempt resolution.
    If it resolves AND `auto_accept_enabled` is still False, the order lands
    in `READY_TO_ACCEPT` (a SECOND explicit accept, same as any other order
    on that connection) rather than the kitchen — this is intentional, not
    a bug: fixing a mapping is a different decision from confirming the
    kitchen can make the food right now.
  - `READY_TO_ACCEPT` — staff explicitly confirming an already-resolved
    order into the kitchen. The re-enqueued job carries `force_accept=True`,
    which bypasses the `auto_accept_enabled` gate for THIS one order only —
    it never touches the connection's own setting.
`auto_accept_enabled` itself is toggled via
`POST /connections/{provider}/auto-accept` below, and is read-only elsewhere
(surfaced on `DeliveryConnectionResponse`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

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
    auto_accept_enabled: bool
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
        auto_accept_enabled=False,
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
        auto_accept_enabled=connection.auto_accept_enabled,
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


def _normalise_unresolved(raw) -> list[dict] | None:
    """
    `{kind, external_id, external_text, ...}` per entry — the shape the
    manager dashboard's "Map this item" deep link reads. Rows written before
    this fix hold bare name strings; those are returned with no id rather
    than dropped, so nothing an operator could act on disappears.
    """
    if raw is None:
        return None
    entries: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            entries.append(entry)
        elif isinstance(entry, str):
            entries.append({"kind": None, "external_id": None, "external_text": entry})
    return entries


def _snapshot(row: DeliveryOrder) -> dict:
    return row.fetched_order_snapshot_json if isinstance(row.fetched_order_snapshot_json, dict) else {}


class DeliveryOrderResponse(BaseModel):
    id: uuid.UUID
    provider: str
    provider_label: str
    external_order_id: str
    display_id: str | None = None
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
    last_provider_sync_at: datetime | None = None
    last_provider_sync_error: str | None = None
    # What was ordered (titles/quantities/modifiers), from the allow-listed
    # fetch snapshot — no phone number, no address.
    lines: list | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: DeliveryOrder) -> "DeliveryOrderResponse":
        return cls(
            id=row.id,
            provider=row.provider,
            provider_label=PROVIDER_LABELS.get(row.provider, row.provider),
            external_order_id=row.external_order_id,
            display_id=getattr(row, "display_id", None),
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
            unresolved_items=_normalise_unresolved(row.unresolved_items_json),
            failure_reason=row.failure_reason,
            last_provider_sync_at=getattr(row, "last_provider_sync_at", None),
            last_provider_sync_error=getattr(row, "last_provider_sync_error", None),
            lines=_snapshot(row).get("lines"),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _order_response_for(user: AuthUser, row: DeliveryOrder) -> DeliveryOrderResponse:
    """
    Response for an action STAFF may take (accept / deny / retry-accept).
    Staff get the order's state, not the platform's payout figures — the
    same rule as the staff queue view (DeliveryQueueItem); owners and
    managers get everything.
    """
    response = DeliveryOrderResponse.from_row(row)
    if user.role in ("OWNER", "MANAGER"):
        return response
    response.gross_cents = None
    response.fee_cents = None
    if response.lines:
        response.lines = [
            {**line, "unit_price_cents": None, "total_price_cents": None}
            for line in response.lines if isinstance(line, dict)
        ]
    return response


class DeliveryQueueItem(BaseModel):
    """
    The STAFF view of a delivery order still waiting on someone — what was
    ordered, how long is left, and what (if anything) staff may do about
    it. Deliberately narrower than DeliveryOrderResponse: no provider
    payout figures, no failure internals beyond a plain reason, no mapping
    ids (staff cannot map; managers can).
    """
    id: uuid.UUID
    provider: str
    provider_label: str
    display_id: str | None
    status: str
    placed_at: datetime | None
    accept_deadline_at: datetime | None
    customer_first_name: str | None
    special_instructions: str | None
    lines: list
    unresolved: list[str]
    can_accept: bool
    can_deny: bool
    can_retry_accept: bool
    waiting_for_manager: bool
    message: str | None

    @classmethod
    def from_row(cls, row: DeliveryOrder) -> "DeliveryQueueItem":
        snap = _snapshot(row)
        unresolved = [
            str(e.get("external_text") or "")
            for e in (_normalise_unresolved(row.unresolved_items_json) or [])
            if e.get("external_text")
        ]
        status = row.status
        message = None
        if status == "NEEDS_MAPPING":
            message = "A manager must match these items to your menu before this order can be accepted."
        elif status == "ACCEPT_UNCONFIRMED":
            message = (
                "In the kitchen, but the delivery platform has not confirmed it. "
                "Check the platform's tablet, then retry."
            )
        elif status in ("RECEIVED", "FETCHING"):
            message = "Loading the order from the delivery platform…"
        return cls(
            id=row.id,
            provider=row.provider,
            provider_label=PROVIDER_LABELS.get(row.provider, row.provider),
            display_id=getattr(row, "display_id", None) or snap.get("display_id"),
            status=status,
            placed_at=row.placed_at,
            accept_deadline_at=row.accept_deadline_at,
            customer_first_name=snap.get("customer_first_name"),
            special_instructions=snap.get("special_instructions"),
            lines=[
                {
                    "title": line.get("title"),
                    "quantity": line.get("quantity"),
                    "special_instructions": line.get("special_instructions"),
                    "modifiers": [
                        {"title": m.get("title"), "quantity": m.get("quantity"), "removed": m.get("removed")}
                        for m in (line.get("modifiers") or [])
                    ],
                }
                for line in (snap.get("lines") or [])
            ],
            unresolved=unresolved,
            can_accept=status == "READY_TO_ACCEPT",
            can_deny=status in _OPEN_ORDER_STATUSES,
            can_retry_accept=status == "ACCEPT_UNCONFIRMED",
            waiting_for_manager=status == "NEEDS_MAPPING",
            message=message,
        )


class DeliveryOrderListResponse(BaseModel):
    orders: list[DeliveryOrderResponse]
    total: int
    limit: int
    offset: int


class AutoAcceptRequest(BaseModel):
    enabled: bool


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
    # The channel's own price for this entity (from the last catalogue
    # import) beside this restaurant's in-store price, so a manager can see
    # where Uber prices differ. Both informational — orders are always booked
    # at the price the provider actually charged.
    provider_price_cents: int | None = None
    provider_group_title: str | None = None
    menu_price_cents: int | None = None


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


def _mapping_response(
    m: ExternalProductMapping, item_names: dict, add_on_names: dict, menu_prices: dict | None = None,
) -> MappingResponse:
    snapshot = m.provider_snapshot_json if isinstance(m.provider_snapshot_json, dict) else {}
    menu_prices = menu_prices or {}
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
        provider_price_cents=snapshot.get("price_cents"),
        provider_group_title=snapshot.get("group_title"),
        menu_price_cents=menu_prices.get(m.menu_add_on_id or m.menu_item_id),
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


@router.post("/connections/{provider}/auto-accept", response_model=DeliveryConnectionResponse)
async def set_auto_accept(
    provider: str,
    body: AutoAcceptRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Deliberately opt in (or back out of) zero-touch kitchen commitment
    (migration 0046). Defaults FALSE for every connection — a manager must
    turn this on explicitly. When False, a fully-mapped order stops at
    READY_TO_ACCEPT and needs an explicit staff accept
    (POST .../orders/{id}/accept) before it reaches the kitchen; see
    order_service.py::_handle_order.
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

    previous = connection.auto_accept_enabled
    connection.auto_accept_enabled = body.enabled
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.connection.auto_accept_changed", target_type="delivery_connection",
        target_id=connection.id,
        diff_json={"provider": provider, "previous": previous, "new": body.enabled},
    ))
    await db.commit()
    return await _connection_response(db, user.business_id, connection)


# ── Store status (pause / resume on the provider) ────────────────────────────

class StoreStatusRequest(BaseModel):
    status: str = Field(pattern="^(ONLINE|PAUSED)$")
    paused_until: datetime | None = None
    reason: str | None = Field(default=None, max_length=200)


class StoreStatusResponse(BaseModel):
    provider: str
    store_status: str | None
    last_status_synced_at: datetime | None


async def _connection_for_provider_call(
    db: AsyncSession, business_id: uuid.UUID, provider: str,
) -> DeliveryConnection:
    if provider not in PROVIDER_LABELS:
        raise AppError("UNKNOWN_PROVIDER", f"Unknown provider {provider!r}", 422)
    connection = (
        await db.execute(
            select(DeliveryConnection).where(
                DeliveryConnection.business_id == business_id,
                DeliveryConnection.provider == provider,
            )
        )
    ).scalar_one_or_none()
    if connection is None or not connection.external_store_id:
        raise NotFoundError("Delivery connection", provider)
    if connection.status in ("NOT_CONNECTED", "DISABLED"):
        raise AppError(
            "DELIVERY_CONNECTION_INACTIVE",
            f"This connection is {connection.status} — nothing to change on the provider.",
            409,
        )
    return connection


def _provider_call_error(provider: str, exc: Exception) -> AppError:
    from backend.app.delivery.errors import DeliveryCapabilityUnsupported

    label = PROVIDER_LABELS.get(provider, provider)
    if isinstance(exc, DeliveryCapabilityUnsupported):
        return AppError(
            "DELIVERY_CAPABILITY_UNSUPPORTED",
            f"{label} does not support this from NextGen — use {label}'s own tools.", 409,
        )
    return AppError(
        "DELIVERY_PROVIDER_UNAVAILABLE",
        f"Could not reach {label} right now — nothing was changed. Try again shortly.", 503,
    )


@router.get("/connections/{provider}/store-status", response_model=StoreStatusResponse)
async def get_store_status(
    provider: str,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Read the store's live/paused state from the provider and cache it."""
    from backend.app.delivery import registry
    from backend.app.delivery.errors import DeliveryError

    await _require_delivery_module_enabled(db, user.business_id)
    connection = await _connection_for_provider_call(db, user.business_id, provider)
    adapter = registry.get_provider(provider)
    try:
        raw = await adapter.get_store_status(context=_connection_context(connection))
    except DeliveryError as exc:
        raise _provider_call_error(provider, exc) from exc
    status_value = raw.get("status") if isinstance(raw, dict) else None
    connection.store_status = str(status_value)[:16] if status_value else None
    connection.last_status_synced_at = datetime.now(timezone.utc)
    await db.commit()
    return StoreStatusResponse(
        provider=provider, store_status=connection.store_status,
        last_status_synced_at=connection.last_status_synced_at,
    )


@router.post("/connections/{provider}/store-status", response_model=StoreStatusResponse)
async def set_store_status(
    provider: str,
    body: StoreStatusRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Pause ("too busy") or resume the store on the provider. Store-level
    availability — the provider's own control, so pausing here actually
    stops new orders, unlike a NextGen-side flag the provider never sees.
    """
    from backend.app.delivery import registry
    from backend.app.delivery.errors import DeliveryError

    await _require_delivery_module_enabled(db, user.business_id)
    connection = await _connection_for_provider_call(db, user.business_id, provider)
    adapter = registry.get_provider(provider)
    previous = connection.store_status
    try:
        await adapter.set_store_status(
            context=_connection_context(connection),
            status=body.status,
            paused_until=body.paused_until.isoformat() if body.paused_until else None,
            reason=body.reason,
        )
    except DeliveryError as exc:
        raise _provider_call_error(provider, exc) from exc
    connection.store_status = body.status
    connection.last_status_synced_at = datetime.now(timezone.utc)
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.connection.store_status_changed", target_type="delivery_connection",
        target_id=connection.id,
        diff_json={
            "provider": provider, "previous": previous, "new": body.status,
            "paused_until": body.paused_until.isoformat() if body.paused_until else None,
        },
    ))
    await db.commit()
    return StoreStatusResponse(
        provider=provider, store_status=connection.store_status,
        last_status_synced_at=connection.last_status_synced_at,
    )


def _connection_context(connection: DeliveryConnection):
    from backend.app.delivery.base import DeliveryOrderContext

    return DeliveryOrderContext(
        business_id=connection.business_id,
        connection_id=connection.id,
        external_store_id=connection.external_store_id,
        environment=connection.environment,
    )


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
    See module docstring. Valid from NEEDS_MAPPING (re-attempt resolution)
    or READY_TO_ACCEPT (staff explicitly confirming an already-resolved
    order into the kitchen — `auto_accept_enabled=False` path, migration
    0046). Resets the row to RECEIVED and re-enqueues the SAME
    process_delivery_order ARQ job the webhook plane uses — no parallel
    accept logic in this route.
    """
    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(
        db, business_id=user.business_id, order_id=order_id, for_update=True
    )
    if row.status not in ("NEEDS_MAPPING", "READY_TO_ACCEPT"):
        raise AppError(
            "DELIVERY_ORDER_NOT_ACCEPTABLE",
            f"This order is {row.status}, not NEEDS_MAPPING or READY_TO_ACCEPT — "
            "nothing to manually accept.",
            409,
        )

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise AppError(
            "DELIVERY_WORKER_UNAVAILABLE",
            "Could not queue this order for processing right now — please try again shortly.",
            503,
        )

    # force_accept=True ONLY for a staff-confirmed READY_TO_ACCEPT order —
    # bypasses the auto_accept_enabled gate for this one order, never the
    # connection's own setting. A NEEDS_MAPPING re-attempt gets no such
    # bypass: resolving a mapping is a different decision from confirming
    # the kitchen can make the food right now (see module docstring).
    force_accept = row.status == "READY_TO_ACCEPT"

    order_service._transition(
        db, row, "RECEIVED", actor_user_id=user.user_id,
        action="delivery.order.manual_accept_requested",
        extra={"force_accept": force_accept},
    )
    await db.commit()

    try:
        await arq_pool.enqueue_job(
            "process_delivery_order", row.provider, str(row.business_id),
            row.external_order_id, None, force_accept,
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
    return _order_response_for(user, row)


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
        actor_user_id=user.user_id,
        action="delivery.order.manual_deny",
    )
    await db.commit()
    await db.refresh(row)
    return _order_response_for(user, row)


# ── Staff queue (Staff PWA) ──────────────────────────────────────────────────

_QUEUE_STATUSES = (*_OPEN_ORDER_STATUSES, "ACCEPT_UNCONFIRMED")


@router.get("/queue", response_model=list[DeliveryQueueItem])
async def staff_queue(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Delivery orders still waiting on someone, for the kitchen/till staff.

    Closes the "one kitchen queue" gap (DECISIONS.md DECISION-0055): staff
    could accept/deny but had no way to SEE a pending delivery order. This
    is a narrow, read-only, staff-safe projection — the full owner/manager
    list (`GET /orders`) keeps its role gate unchanged. Pending orders have
    no `Order` row yet, so nothing here can be prepared by mistake: only an
    accepted order ever reaches the kitchen queue (`GET /v1/business/orders`).
    """
    await _require_delivery_module_enabled(db, user.business_id)
    rows = (
        await db.execute(
            select(DeliveryOrder)
            .where(
                DeliveryOrder.business_id == user.business_id,
                DeliveryOrder.status.in_(_QUEUE_STATUSES),
            )
            .order_by(DeliveryOrder.accept_deadline_at.asc().nulls_last(), DeliveryOrder.created_at.asc())
            .limit(50)
        )
    ).scalars().all()
    return [DeliveryQueueItem.from_row(r) for r in rows]


# ── ACCEPT_UNCONFIRMED resolution ────────────────────────────────────────────

@router.post("/orders/{order_id}/retry-accept", response_model=DeliveryOrderResponse)
async def retry_accept(
    order_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Re-send the accept for an order whose original accept went unanswered.
    The sale and the kitchen ticket already exist — this only repeats the
    provider handshake, once, and never creates or removes a sale.
    """
    from backend.app.delivery import registry
    from backend.app.delivery.errors import (
        DeliveryProviderError,
        DeliveryProviderNotImplemented,
        DeliveryProviderTimeout,
    )

    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(db, business_id=user.business_id, order_id=order_id, for_update=True)
    if row.status != "ACCEPT_UNCONFIRMED":
        raise AppError(
            "DELIVERY_ORDER_NOT_UNCONFIRMED",
            f"This order is {row.status} — only an unconfirmed accept can be retried.",
            409,
        )
    connection = (
        await db.get(DeliveryConnection, row.delivery_connection_id) if row.delivery_connection_id else None
    )
    adapter = registry.get_provider(row.provider)
    try:
        await adapter.accept_order(
            external_order_id=row.external_order_id,
            context=order_service._context_for(row, connection),
            our_order_id=str(row.order_id),
        )
    except DeliveryProviderError as exc:
        row.last_provider_sync_at = datetime.now(timezone.utc)
        row.last_provider_sync_error = f"Retry accept failed: {exc}"
        order_service._audit(
            db, row, "delivery.order.retry_accept_failed",
            actor_user_id=user.user_id, extra={"status_code": exc.status_code},
        )
        await db.commit()
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            raise AppError(
                "DELIVERY_ACCEPT_REFUSED",
                f"{PROVIDER_LABELS.get(row.provider, row.provider)} refused the accept "
                f"(HTTP {exc.status_code}) — the order may have been cancelled there. "
                "Check the platform's tablet before preparing it.",
                409,
            )
        raise AppError(
            "DELIVERY_PROVIDER_UNAVAILABLE",
            "The delivery platform did not answer — try again shortly.", 503,
        )
    except (DeliveryProviderTimeout, DeliveryProviderNotImplemented):
        raise AppError(
            "DELIVERY_PROVIDER_UNAVAILABLE",
            "The delivery platform did not answer — try again shortly.", 503,
        )

    order_service._transition(
        db, row, "ACCEPTED", actor_user_id=user.user_id,
        action="delivery.order.retry_accept_succeeded",
    )
    row.accepted_at = datetime.now(timezone.utc)
    row.failure_reason = None
    await db.commit()
    await db.refresh(row)
    return _order_response_for(user, row)


@router.post("/orders/{order_id}/mark-accepted", response_model=DeliveryOrderResponse)
async def mark_accepted(
    order_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    A manager confirms, from the provider's own tablet/portal, that the
    provider DID accept an order NextGen never got an answer for. Recorded
    with the manager as the actor; no provider call is made.
    """
    await _require_delivery_module_enabled(db, user.business_id)
    row = await _load_order_or_404(db, business_id=user.business_id, order_id=order_id, for_update=True)
    if row.status != "ACCEPT_UNCONFIRMED":
        raise AppError(
            "DELIVERY_ORDER_NOT_UNCONFIRMED",
            f"This order is {row.status} — only an unconfirmed accept can be marked accepted.",
            409,
        )
    order_service._transition(
        db, row, "ACCEPTED", actor_user_id=user.user_id,
        action="delivery.order.marked_accepted_by_manager",
        reason="confirmed on the provider's side by a manager",
    )
    row.accepted_at = datetime.now(timezone.utc)
    row.failure_reason = None
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

    items = (
        await db.execute(select(MenuItem).where(MenuItem.business_id == user.business_id))
    ).scalars().all()
    add_on_rows = (
        await db.execute(select(MenuAddOn).where(MenuAddOn.business_id == user.business_id))
    ).scalars().all()
    item_names = {m.id: m.name for m in items}
    add_on_names = {a.id: a.name for a in add_on_rows}
    menu_prices = {m.id: m.price_cents for m in items}
    menu_prices.update({a.id: a.price_cents for a in add_on_rows})
    return [_mapping_response(m, item_names, add_on_names, menu_prices) for m in rows]


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


# ── Catalogue import (read the provider's menu into the mapping screen) ──────

class CatalogueImportRequest(BaseModel):
    provider: str = Field(pattern="^UBER_EATS$")


class CatalogueImportResponse(BaseModel):
    provider: str
    fetched: int
    created: int
    auto_mapped: int
    adopted_from_csv: int
    renamed: int
    unchanged: int
    name_collisions: int


@router.post("/menu-mappings/import", response_model=CatalogueImportResponse)
async def import_provider_catalogue(
    body: CatalogueImportRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Read (never write) the provider's menu and make sure every item and
    modifier has a mapping row keyed by the provider's own id, so a manager
    can map the whole menu BEFORE go-live instead of one order at a time
    (Uber: every auto-deny counts against the store's quality bar).

    Conservative by the same rule as every importer here: an exact
    normalised-name match to a menu item (ITEM) or add-on (MODIFIER) is
    mapped automatically; anything else is left UNMAPPED for a human. An
    existing CSV-created mapping with the same name is ADOPTED (given the
    provider id) rather than duplicated, so the two importers never
    disagree. A provider entity whose name changed since it was mapped is
    flagged CHANGED for review — its target is kept, never silently moved.
    Works on SANDBOX / AWAITING_APPROVAL connections on purpose: mapping is
    meant to be finished before a store goes live.
    """
    from backend.app.delivery import registry
    from backend.app.delivery.errors import DeliveryError
    from backend.app.external_orders.service import normalise_key

    await _require_delivery_module_enabled(db, user.business_id)
    connection = await _connection_for_provider_call(db, user.business_id, body.provider)
    adapter = registry.get_provider(body.provider)
    try:
        entries = await adapter.fetch_menu_catalogue(context=_connection_context(connection))
    except DeliveryError as exc:
        raise _provider_call_error(body.provider, exc) from exc

    existing = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.business_id == user.business_id,
                ExternalProductMapping.platform == body.provider,
            )
        )
    ).scalars().all()
    by_external_id = {(m.kind, m.external_id): m for m in existing if m.external_id}
    by_key = {(m.kind, m.external_key): m for m in existing}

    items = (
        await db.execute(
            select(MenuItem).where(MenuItem.business_id == user.business_id, MenuItem.is_deleted.is_(False))
        )
    ).scalars().all()
    add_on_rows = (
        await db.execute(
            select(MenuAddOn).where(MenuAddOn.business_id == user.business_id, MenuAddOn.is_deleted.is_(False))
        )
    ).scalars().all()
    item_by_name = {normalise_key(m.name): m for m in items}
    add_on_by_name = {normalise_key(a.name): a for a in add_on_rows}

    now = datetime.now(timezone.utc)
    counts = dict(created=0, auto_mapped=0, adopted_from_csv=0, renamed=0, unchanged=0, name_collisions=0)

    for entry in entries:
        snapshot = {"title": entry.title, "price_cents": entry.price_cents, "group_title": entry.group_title}
        key = normalise_key(entry.title)[:500]
        mapping = by_external_id.get((entry.kind, entry.external_id))

        if mapping is not None:
            previous_title = (mapping.provider_snapshot_json or {}).get("title") if isinstance(
                mapping.provider_snapshot_json, dict) else None
            if previous_title and normalise_key(previous_title) != normalise_key(entry.title) \
                    and mapping.status == "MAPPED":
                mapping.status = "CHANGED"
                mapping.status_reason = (
                    f"Renamed on the provider from {previous_title!r} to {entry.title!r} — "
                    "check it still means the same thing on your menu."
                )
                counts["renamed"] += 1
            else:
                counts["unchanged"] += 1
            mapping.provider_snapshot_json = snapshot
            mapping.last_seen_at = now
            mapping.delivery_connection_id = connection.id
            continue

        csv_row = by_key.get((entry.kind, key))
        if csv_row is not None and csv_row.external_id is None:
            csv_row.external_id = entry.external_id[:128]
            csv_row.provider_snapshot_json = snapshot
            csv_row.last_seen_at = now
            csv_row.delivery_connection_id = connection.id
            by_external_id[(entry.kind, entry.external_id)] = csv_row
            counts["adopted_from_csv"] += 1
            continue

        status_reason = None
        if csv_row is not None:
            # Another provider entity already uses this exact name. The text
            # key must stay unique, so this one is stored under a
            # disambiguated key and left for a human to map.
            key = f"{key} [{entry.external_id}]"[:500]
            status_reason = "Another provider item has the same name — map this one by hand."
            counts["name_collisions"] += 1

        mapping = ExternalProductMapping(
            business_id=user.business_id,
            platform=body.provider,
            kind=entry.kind,
            external_text=entry.title[:500],
            external_key=key,
            external_id=entry.external_id[:128],
            status="UNMAPPED",
            status_reason=status_reason,
            provider_snapshot_json=snapshot,
            delivery_connection_id=connection.id,
            last_seen_at=now,
            created_by_user_id=user.user_id,
        )
        if status_reason is None:
            target_name = normalise_key(entry.title)
            if entry.kind == ExternalMappingKind.ITEM.value and target_name in item_by_name:
                mapping.menu_item_id = item_by_name[target_name].id
            elif entry.kind == ExternalMappingKind.MODIFIER.value and target_name in add_on_by_name:
                mapping.menu_add_on_id = add_on_by_name[target_name].id
            if mapping.menu_item_id or mapping.menu_add_on_id:
                mapping.status = "MAPPED"
                mapping.status_reason = "Matched automatically by exact name."
                counts["auto_mapped"] += 1
        db.add(mapping)
        by_external_id[(entry.kind, entry.external_id)] = mapping
        by_key[(entry.kind, key)] = mapping
        counts["created"] += 1

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action="delivery.menu_mappings.catalogue_imported", target_type="delivery_connection",
        target_id=connection.id,
        diff_json={"provider": body.provider, "fetched": len(entries), **counts},
    ))
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError("mapping", "external id / normalised text") from exc

    return CatalogueImportResponse(provider=body.provider, fetched=len(entries), **counts)


# ── Health (connection + order-flow summary for the dashboard) ───────────────

class ProviderHealth(BaseModel):
    provider: str
    provider_label: str
    connection_status: str
    environment: str | None
    needs_attention: bool
    error_reason: str | None
    store_status: str | None
    last_webhook_at: datetime | None
    open_orders: int
    needs_mapping: int
    ready_to_accept: int
    accept_unconfirmed: int
    provider_sync_errors: int
    accepted_24h: int
    denied_24h: int
    failed_24h: int
    # accepted / (accepted + denied + failed) over 24h — the same measure as
    # the provider's own order-injection success rate (Uber: 99.9% target,
    # access can be revoked below 99%). None when there were no orders.
    acceptance_rate_24h: float | None
    unmapped_menu_entries: int


@router.get("/health", response_model=list[ProviderHealth])
async def delivery_health(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_delivery_module_enabled(db, user.business_id)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    connections = (
        await db.execute(
            select(DeliveryConnection).where(DeliveryConnection.business_id == user.business_id)
        )
    ).scalars().all()

    open_counts = (
        await db.execute(
            select(DeliveryOrder.provider, DeliveryOrder.status, func.count())
            .where(
                DeliveryOrder.business_id == user.business_id,
                DeliveryOrder.status.in_(_QUEUE_STATUSES),
            )
            .group_by(DeliveryOrder.provider, DeliveryOrder.status)
        )
    ).all()
    recent_counts = (
        await db.execute(
            select(DeliveryOrder.provider, DeliveryOrder.status, func.count())
            .where(DeliveryOrder.business_id == user.business_id, DeliveryOrder.created_at >= since)
            .group_by(DeliveryOrder.provider, DeliveryOrder.status)
        )
    ).all()
    sync_errors = dict(
        (
            await db.execute(
                select(DeliveryOrder.provider, func.count())
                .where(
                    DeliveryOrder.business_id == user.business_id,
                    DeliveryOrder.last_provider_sync_error.isnot(None),
                    DeliveryOrder.updated_at >= since,
                )
                .group_by(DeliveryOrder.provider)
            )
        ).all()
    )
    unmapped = dict(
        (
            await db.execute(
                select(ExternalProductMapping.platform, func.count())
                .where(
                    ExternalProductMapping.business_id == user.business_id,
                    ExternalProductMapping.external_id.isnot(None),
                    ExternalProductMapping.status.in_(("UNMAPPED", "CHANGED", "INVALID")),
                )
                .group_by(ExternalProductMapping.platform)
            )
        ).all()
    )
    last_webhooks = dict(
        (
            await db.execute(
                select(DeliveryWebhookEvent.provider, func.max(DeliveryWebhookEvent.received_at))
                .where(DeliveryWebhookEvent.business_id == user.business_id)
                .group_by(DeliveryWebhookEvent.provider)
            )
        ).all()
    )

    open_by = {}
    for provider, status_value, count in open_counts:
        open_by.setdefault(provider, {})[status_value] = count
    recent_by = {}
    for provider, status_value, count in recent_counts:
        recent_by.setdefault(provider, {})[status_value] = count

    result = []
    for c in connections:
        opens = open_by.get(c.provider, {})
        recent = recent_by.get(c.provider, {})
        accepted = sum(recent.get(s, 0) for s in ("ACCEPTED", "ACCEPT_UNCONFIRMED", "FULFILLED"))
        denied = recent.get("DENIED", 0)
        failed = recent.get("FAILED", 0)
        decided = accepted + denied + failed
        result.append(ProviderHealth(
            provider=c.provider,
            provider_label=PROVIDER_LABELS.get(c.provider, c.provider),
            connection_status=c.status,
            environment=c.environment,
            needs_attention=c.status == "ATTENTION_REQUIRED" or bool(opens.get("ACCEPT_UNCONFIRMED")),
            error_reason=c.error_reason,
            store_status=c.store_status,
            last_webhook_at=last_webhooks.get(c.provider),
            open_orders=sum(opens.values()),
            needs_mapping=opens.get("NEEDS_MAPPING", 0),
            ready_to_accept=opens.get("READY_TO_ACCEPT", 0),
            accept_unconfirmed=opens.get("ACCEPT_UNCONFIRMED", 0),
            provider_sync_errors=sync_errors.get(c.provider, 0),
            accepted_24h=accepted,
            denied_24h=denied,
            failed_24h=failed,
            acceptance_rate_24h=(round(accepted / decided, 4) if decided else None),
            unmapped_menu_entries=unmapped.get(c.provider, 0),
        ))
    return result
