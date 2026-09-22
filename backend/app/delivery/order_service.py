"""
Worker plane — the SLOW half of the order flow (design doc §5). Two ARQ job
entry points (`process_delivery_order`, `process_delivery_cancellation`,
both registered in `backend/app/worker.py`) plus the shared implementation
they call, and the accept-deadline watchdog's own logic.

GATES 4-6 LIVE HERE, NOT IN `intake.py`
--------------------------------------------
`intake.py`'s module docstring explains the split: environment match,
tenant active, entitlement, and connection status are all checked here,
FIRST, before any Get Order Details call — and any of the four failing
results in an ACTIVE `deny_pos_order` call (design §7: "a known store ...
gets an active, prompt deny_pos_order, not silence"), never a silent drop.

IDEMPOTENCY — ROW LOCK + STATE RE-CHECK (design §3)
---------------------------------------------------------
`SELECT ... FOR UPDATE` on the `delivery_orders` row before any
provider-mutating work, then a re-check that `status` is still one this
call is allowed to act from. Combined with `intake.py`'s two
`ON CONFLICT DO NOTHING` inserts, this is the third of three stacked
uniqueness guarantees the design doc describes: even if the SAME job were
somehow enqueued twice (e.g. a retried `enqueue_job` call whose first
attempt actually landed), the second invocation finds the row already past
`RECEIVED` and does nothing further.

STOCK / VAT — REUSE, NEVER RE-IMPLEMENT
--------------------------------------------
`_create_order_from_canonical()` below calls the EXACT SAME
`post_sale_consumption()` (`backend/app/inventory/ledger.py`) and
`calculate_vat_cents()` (`shared/pricing/engine.py`) the CSV importer uses,
with the identical "VAT derived from inside the platform's consumer price"
convention (`external_orders/service.py::import_resolved_orders`) — a R149
Uber sale is recorded as R149. `Order.till_session_id` is never set (stays
NULL), so this can never reach till cash-up, same as every existing
external-platform order.

AN UNMAPPED ITEM NEVER AUTO-CREATES A SALE
-----------------------------------------------
If any line/modifier fails to resolve against `external_product_mappings`,
the `DeliveryOrder` row goes to `NEEDS_MAPPING` and NOTHING is written to
`orders`/`order_items` — no Order, no stock deduction. The accept-deadline
watchdog (`delivery_accept_deadline_watchdog`, below) is the only thing
that ever moves a `NEEDS_MAPPING` order forward, and it always denies,
never accepts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.delivery import registry
from backend.app.delivery.base import DeliveryOrderContext
from backend.app.delivery.canonical import CanonicalDeliveryOrder, CanonicalDeliveryLine
from backend.app.delivery.entitlements import is_connection_live, is_provider_entitled
from backend.app.delivery.errors import (
    DeliveryProviderError,
    DeliveryProviderNotImplemented,
    DeliveryProviderTimeout,
)
from backend.app.external_orders.adapters import order_id_prefix
from backend.app.external_orders.service import EXTERNAL_PAYMENT_METHOD, normalise_key
from backend.app.inventory.ledger import post_sale_consumption
from shared.enums import ExternalMappingKind, OrderStatus, PLATFORM_TO_ORDER_SOURCE
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order, OrderEvent, OrderItem
from shared.pricing.engine import calculate_vat_cents

logger = logging.getLogger("nextgen.delivery.order_service")

#: Deny reason codes for OUR OWN misconfiguration gates (design §7) — always
#: STORE_CLOSED, the most honest Uber-defined code for "this restaurant is
#: not actually able to take this order right now," and never a code that
#: implies something about the ITEM (that's ITEM_AVAILABILITY, reserved for
#: a genuinely unmapped/86'd product).
_MISCONFIG_DENY_CODE = "STORE_CLOSED"
_CANCEL_EVENT_TYPES = frozenset({"orders.cancel", "orders.failure"})


# ── ARQ job entry points (registered in backend/app/worker.py) ──────────────


async def process_delivery_order(
    ctx: dict, provider: str, business_id: str, external_order_id: str, event_type: str | None = None,
) -> None:
    """
    ARQ job. Opens its own session — no FastAPI dependency injection. Never
    raises for a business-logic outcome (misconfiguration, unmapped items,
    provider error) — only an infrastructure failure propagates, so ARQ's
    own retry (`max_tries`) applies to THAT, not to a normal denial.
    """
    async with ctx["session_factory"]() as db:
        try:
            await _handle_order(
                db, provider=provider, business_id=uuid.UUID(business_id),
                external_order_id=external_order_id, event_type=event_type,
            )
        except (DeliveryProviderTimeout,):
            await db.rollback()
            logger.warning(
                "DELIVERY_ORDER_TRANSIENT_FAILURE: provider=%s business_id=%s "
                "external_order_id=%s — will be retried.",
                provider, business_id, external_order_id,
            )
            raise
        except Exception:
            await db.rollback()
            logger.exception(
                "DELIVERY_ORDER_PROCESSING_FAILED: provider=%s business_id=%s external_order_id=%s",
                provider, business_id, external_order_id,
            )


async def process_delivery_cancellation(
    ctx: dict, provider: str, business_id: str, external_order_id: str,
) -> None:
    """
    ARQ job for `orders.cancel` / `orders.failure`. A financial cancellation
    is not a production fact (DECISIONS.md §6, carried over from the CSV
    path unchanged) — this never auto-reverses stock. It marks the
    `delivery_orders` row CANCELLED and, if a real `Order` was already
    created, cancels that Order through the SAME path staff use
    (`OrderEvent` + status, leaving inventory reversal to the existing
    staff-facing cancellation flow rather than guessing here).
    """
    async with ctx["session_factory"]() as db:
        try:
            await _handle_cancellation(
                db, provider=provider, business_id=uuid.UUID(business_id),
                external_order_id=external_order_id,
            )
        except Exception:
            await db.rollback()
            logger.exception(
                "DELIVERY_CANCELLATION_PROCESSING_FAILED: provider=%s business_id=%s "
                "external_order_id=%s", provider, business_id, external_order_id,
            )


# ── Shared implementation ────────────────────────────────────────────────────


async def _load_locked_row(
    db: AsyncSession, *, business_id: uuid.UUID, provider: str, external_order_id: str
) -> DeliveryOrder | None:
    result = await db.execute(
        select(DeliveryOrder)
        .where(
            DeliveryOrder.business_id == business_id,
            DeliveryOrder.provider == provider,
            DeliveryOrder.external_order_id == external_order_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _issue_deny(
    db: AsyncSession,
    *,
    row: DeliveryOrder,
    connection: DeliveryConnection | None,
    reason_code: str,
    explanation: str,
) -> None:
    adapter = registry.get_provider(row.provider)
    context = DeliveryOrderContext(
        business_id=row.business_id,
        connection_id=connection.id if connection else uuid.uuid4(),
        external_store_id=row.external_store_id,
        environment=connection.environment if connection else "production",
    )
    try:
        await adapter.deny_order(
            external_order_id=row.external_order_id, context=context,
            reason_code=reason_code, explanation=explanation,
        )
    except (DeliveryProviderNotImplemented, DeliveryProviderError, DeliveryProviderTimeout):
        logger.exception(
            "DELIVERY_DENY_CALL_FAILED: business=%s external_order_id=%s reason=%s — "
            "marked DENIED locally regardless; Uber's own 11.5-minute timeout is the backstop.",
            row.business_id, row.external_order_id, reason_code,
        )
    row.status = "DENIED"
    row.denied_at = datetime.now(timezone.utc)
    row.deny_reason_code = reason_code
    row.deny_reason_text = explanation


async def _handle_order(
    db: AsyncSession, *, provider: str, business_id: uuid.UUID, external_order_id: str,
    event_type: str | None,
) -> None:
    if event_type in _CANCEL_EVENT_TYPES:
        await _handle_cancellation(
            db, provider=provider, business_id=business_id, external_order_id=external_order_id
        )
        return

    row = await _load_locked_row(
        db, business_id=business_id, provider=provider, external_order_id=external_order_id
    )
    if row is None:
        logger.error(
            "DELIVERY_ORDER_ROW_MISSING: provider=%s business_id=%s external_order_id=%s — "
            "intake.py should have created this row before enqueueing.",
            provider, business_id, external_order_id,
        )
        return
    if row.status != "RECEIVED":
        logger.info(
            "DELIVERY_ORDER_SKIP_ALREADY_IN_PROGRESS: id=%s status=%s — idempotent no-op.",
            row.id, row.status,
        )
        return

    connection = await db.get(DeliveryConnection, row.delivery_connection_id) if row.delivery_connection_id else None
    business = await db.get(Business, business_id)
    webhook_event = await db.get(DeliveryWebhookEvent, row.raw_event_id) if row.raw_event_id else None

    misconfig = _find_misconfiguration(
        connection=connection, business=business, webhook_event=webhook_event, provider=provider,
    )
    if misconfig:
        row.status = "FAILED"
        row.failure_reason = misconfig
        logger.warning(
            "DELIVERY_ORDER_MISCONFIGURED: business=%s external_order_id=%s reason=%s — "
            "issuing an active deny (never silence for a known store).",
            business_id, external_order_id, misconfig,
        )
        await _issue_deny(
            db, row=row, connection=connection, reason_code=_MISCONFIG_DENY_CODE,
            explanation=f"NextGen configuration issue: {misconfig}",
        )
        await db.commit()
        return

    adapter = registry.get_provider(provider)
    context = DeliveryOrderContext(
        business_id=business_id, connection_id=connection.id,
        external_store_id=connection.external_store_id, environment=connection.environment,
    )

    row.status = "FETCHING"
    await db.flush()

    try:
        canonical = await adapter.fetch_order(external_order_id=external_order_id, context=context)
    except DeliveryProviderTimeout:
        # Transient — revert to RECEIVED so a retried job re-enters cleanly,
        # and re-raise so ARQ's max_tries actually retries it.
        row.status = "RECEIVED"
        await db.commit()
        raise
    except DeliveryProviderError as exc:
        row.status = "FAILED"
        row.failure_reason = f"fetch_order failed: {exc}"
        await db.commit()
        logger.exception("DELIVERY_ORDER_FETCH_FAILED: business=%s external_order_id=%s", business_id, external_order_id)
        return

    row.placed_at = canonical.placed_at
    row.gross_cents = canonical.total_cents
    row.fee_cents = canonical.fee_cents
    row.currency = canonical.currency or row.currency
    row.fetched_order_snapshot_json = _build_snapshot(canonical)

    resolution = await _resolve_delivery_lines(
        db, business_id=business_id, provider=provider, canonical=canonical
    )
    if resolution.blocks_import:
        row.status = "NEEDS_MAPPING"
        row.unresolved_items_json = resolution.unresolved_names
        await db.commit()
        logger.warning(
            "DELIVERY_ORDER_NEEDS_MAPPING: business=%s external_order_id=%s unresolved=%s — "
            "never auto-created a sale; surfaced for staff, will auto-deny at deadline if unresolved.",
            business_id, external_order_id, resolution.unresolved_names,
        )
        await _publish_alert(business_id, "delivery_needs_mapping", row, external_order_id)
        return

    order = await _create_order_from_canonical(
        db, business=business, provider=provider, canonical=canonical, resolution=resolution,
    )
    row.order_id = order.id
    await db.flush()

    try:
        await adapter.accept_order(
            external_order_id=external_order_id, context=context, our_order_id=str(order.id),
        )
        row.status = "ACCEPTED"
        row.accepted_at = datetime.now(timezone.utc)
    except (DeliveryProviderNotImplemented, DeliveryProviderError, DeliveryProviderTimeout):
        # The sale already stands — "the food was made" honesty (DECISIONS.md
        # §6, same principle carried over): a provider-communication failure
        # on the ACCEPT CALL is not grounds to un-sell food staff may already
        # be preparing. Flagged loudly for a human; never silently reversed.
        row.failure_reason = "accept_pos_order call failed after the sale was created locally"
        logger.exception(
            "DELIVERY_ACCEPT_CALL_FAILED: business=%s external_order_id=%s order_id=%s — "
            "Order already created; Uber may not know we accepted.",
            business_id, external_order_id, order.id,
        )

    await db.commit()
    await _publish_alert(business_id, "new_order", row, external_order_id, order=order)


async def _handle_cancellation(
    db: AsyncSession, *, provider: str, business_id: uuid.UUID, external_order_id: str,
) -> None:
    row = await _load_locked_row(
        db, business_id=business_id, provider=provider, external_order_id=external_order_id
    )
    if row is None:
        logger.warning(
            "DELIVERY_CANCELLATION_UNKNOWN_ORDER: provider=%s business_id=%s external_order_id=%s",
            provider, business_id, external_order_id,
        )
        return
    if row.status in ("CANCELLED", "DENIED", "FAILED"):
        logger.info("DELIVERY_CANCELLATION_ALREADY_TERMINAL: id=%s status=%s", row.id, row.status)
        return

    row.status = "CANCELLED"
    row.cancelled_at = datetime.now(timezone.utc)

    if row.order_id:
        order = await db.get(Order, row.order_id)
        if order and order.status not in (OrderStatus.CANCELLED.value,):
            old_status = order.status
            order.status = OrderStatus.CANCELLED.value
            order.cancelled_reason = f"Cancelled by {provider} ({external_order_id})"
            db.add(OrderEvent(
                order_id=order.id, business_id=business_id, old_status=old_status,
                new_status=OrderStatus.CANCELLED.value,
                reason="Delivery marketplace reported this order cancelled. Stock is NOT "
                       "automatically reversed — a financial cancellation says nothing about "
                       "whether the kitchen had already made the food (DECISIONS.md).",
            ))

    await db.commit()
    logger.warning(
        "DELIVERY_ORDER_CANCELLED: business=%s external_order_id=%s order_id=%s",
        business_id, external_order_id, row.order_id,
    )


def _find_misconfiguration(
    *, connection: DeliveryConnection | None, business: Business | None,
    webhook_event: DeliveryWebhookEvent | None, provider: str,
) -> str | None:
    if connection is None:
        return "CONNECTION_MISSING"
    if webhook_event and webhook_event.environment and webhook_event.environment != connection.environment:
        return "ENV_MISMATCH"
    if business is None or not business.is_active:
        return "TENANT_INACTIVE"
    if not is_provider_entitled(business, provider):
        return "ENTITLEMENT_OFF"
    if not is_connection_live(connection):
        return "CONNECTION_NOT_LIVE"
    return None


def _build_snapshot(canonical: CanonicalDeliveryOrder) -> dict:
    """Allow-listed snapshot — never the raw provider response (see canonical.py)."""
    return {
        "display_id": canonical.display_id,
        "order_type": canonical.order_type,
        "item_count": canonical.item_count,
        "subtotal_cents": canonical.subtotal_cents,
        "tax_cents": canonical.tax_cents,
        "fee_cents": canonical.fee_cents,
        "total_cents": canonical.total_cents,
        "fulfillment_issues": list(canonical.fulfillment_issues),
        "provider_metadata": canonical.provider_metadata,
    }


async def _publish_alert(
    business_id: uuid.UUID, alert_type: str, row: DeliveryOrder, external_order_id: str, *, order=None,
) -> None:
    """
    Fire-and-forget: the existing Redis pub/sub channel the Staff PWA's SSE
    stream already subscribes to (`orders:{business_id}`, same channel
    `routes_orders.py` publishes "new_order" on), plus the existing Web Push
    service. Never allowed to fail the caller's transaction — both are
    best-effort notifications, not the record of truth (that's the DB row).
    """
    import json

    try:
        from backend.app.db.session import get_redis
        redis = await get_redis()
        await redis.publish(
            f"orders:{business_id}",
            json.dumps({
                "type": alert_type,
                "delivery_order_id": str(row.id),
                "provider": row.provider,
                "external_order_id": external_order_id,
                "order_id": str(order.id) if order else None,
                "order_number": order.order_number if order else None,
            }),
        )
    except Exception:
        logger.warning("DELIVERY_ALERT_PUBLISH_FAILED: business=%s type=%s", business_id, alert_type)

    try:
        from backend.app.push_service import send_order_alert
        await send_order_alert(
            business_id,
            (order.order_number if order else f"Delivery {external_order_id}"),
            "DELIVERY",
        )
    except Exception:
        logger.warning("DELIVERY_ALERT_PUSH_FAILED: business=%s type=%s", business_id, alert_type)


# ── Mapping resolution (external_id-keyed — see design §6) ──────────────────


@dataclass
class _ResolvedDeliveryLine:
    canonical: CanonicalDeliveryLine
    menu_item_id: uuid.UUID | None = None
    menu_item_name: str | None = None
    add_on_ids: list[uuid.UUID] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    is_ignored: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.is_ignored or self.menu_item_id is not None


@dataclass
class _ResolutionResult:
    lines: list[_ResolvedDeliveryLine] = field(default_factory=list)

    @property
    def blocks_import(self) -> bool:
        return any((not l.is_resolved) or l.unresolved for l in self.lines)

    @property
    def unresolved_names(self) -> list[str]:
        names: list[str] = []
        for line in self.lines:
            if not line.is_resolved:
                names.append(line.canonical.title)
            names.extend(line.unresolved)
        return names


async def _resolve_delivery_lines(
    db: AsyncSession, *, business_id: uuid.UUID, provider: str, canonical: CanonicalDeliveryOrder,
) -> _ResolutionResult:
    """
    Resolve each canonical line against `external_product_mappings`, keyed
    by the provider's STABLE `external_id` (migration 0043) — not the
    normalised-TEXT key the CSV path uses, since a live API order carries a
    real id rather than only free text. Auto-match is otherwise exactly as
    conservative as the CSV path (design §6): an exact saved mapping, or an
    exact menu-item/add-on NAME match. Nothing fuzzier.
    """
    mapping_rows = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.business_id == business_id,
                ExternalProductMapping.platform == provider,
                ExternalProductMapping.external_id.isnot(None),
            )
        )
    ).scalars().all()
    item_by_external_id = {
        m.external_id: m for m in mapping_rows if m.kind == ExternalMappingKind.ITEM.value
    }
    modifier_by_external_id = {
        m.external_id: m for m in mapping_rows if m.kind == ExternalMappingKind.MODIFIER.value
    }

    menu_items = (
        await db.execute(
            select(MenuItem).where(MenuItem.business_id == business_id, MenuItem.is_deleted.is_(False))
        )
    ).scalars().all()
    item_by_id = {m.id: m for m in menu_items}
    item_by_name = {normalise_key(m.name): m for m in menu_items}

    add_ons = (
        await db.execute(
            select(MenuAddOn).where(MenuAddOn.business_id == business_id, MenuAddOn.is_deleted.is_(False))
        )
    ).scalars().all()
    add_on_by_id = {a.id: a for a in add_ons}
    add_on_by_name = {normalise_key(a.name): a for a in add_ons}

    resolved_lines: list[_ResolvedDeliveryLine] = []
    for line in canonical.lines:
        resolved = _ResolvedDeliveryLine(canonical=line)
        mapping = item_by_external_id.get(line.external_item_id) if line.external_item_id else None

        if mapping and mapping.is_ignored:
            resolved.is_ignored = True
        elif mapping and mapping.menu_item_id in item_by_id:
            menu_item = item_by_id[mapping.menu_item_id]
            resolved.menu_item_id = menu_item.id
            resolved.menu_item_name = menu_item.name
        elif normalise_key(line.title) in item_by_name:
            menu_item = item_by_name[normalise_key(line.title)]
            resolved.menu_item_id = menu_item.id
            resolved.menu_item_name = menu_item.name

        for modifier in line.modifiers:
            mod_mapping = (
                modifier_by_external_id.get(modifier.external_id) if modifier.external_id else None
            )
            if mod_mapping and mod_mapping.is_ignored:
                continue
            if mod_mapping and mod_mapping.menu_add_on_id in add_on_by_id:
                resolved.add_on_ids.append(mod_mapping.menu_add_on_id)
                continue
            if normalise_key(modifier.title) in add_on_by_name:
                resolved.add_on_ids.append(add_on_by_name[normalise_key(modifier.title)].id)
                continue
            resolved.unresolved.append(modifier.title)

        resolved_lines.append(resolved)

    return _ResolutionResult(lines=resolved_lines)


# ── Order creation (reuses the SAME sale/stock/VAT pipeline as the CSV path) ─


async def _create_order_from_canonical(
    db: AsyncSession, *, business: Business, provider: str,
    canonical: CanonicalDeliveryOrder, resolution: _ResolutionResult,
) -> Order:
    now = datetime.now(timezone.utc)
    gross = canonical.total_cents or 0
    vat_cents = (
        calculate_vat_cents(gross, business.vat_rate_basis_points, True)
        if getattr(business, "vat_registered", False) else 0
    )
    source = PLATFORM_TO_ORDER_SOURCE.get(provider, "OTHER_EXTERNAL")

    order = Order(
        business_id=business.id,
        order_number=f"{order_id_prefix(provider)}-{canonical.external_order_id}"[:32],
        status=OrderStatus.ACCEPTED.value,
        payment_status="PAID",
        payment_required=False,
        payment_method=EXTERNAL_PAYMENT_METHOD,
        paid_at=canonical.placed_at or now,
        till_session_id=None,
        cash_tendered_cents=None,
        change_due_cents=None,
        source=source,
        order_mode="DELIVERY",
        subtotal_cents=gross,
        delivery_fee_cents=0,
        discount_cents=0,
        tax_cents=vat_cents,
        total_cents=gross,
        currency=canonical.currency or getattr(business, "currency", "ZAR") or "ZAR",
        customer_name=canonical.customer_first_name,
        phone_number=canonical.customer_phone,
        delivery_address=None,  # never available from the provider — see canonical.py
        special_instructions=canonical.special_instructions,
        external_order_id=canonical.external_order_id,
        external_placed_at=canonical.placed_at,
        external_imported_at=now,
        external_status=canonical.provider_status_raw,
        confirmed_at=canonical.placed_at or now,
        accepted_at=now,
    )
    db.add(order)
    await db.flush()

    resolved_items = []
    for line in resolution.lines:
        if line.is_ignored or not line.menu_item_id:
            continue

        add_ons_snapshot = [
            {"add_on_id": str(a), "name": "", "quantity": 1, "price_cents": 0}
            for a in line.add_on_ids
        ]
        db.add(OrderItem(
            order_id=order.id,
            business_id=business.id,
            menu_item_id=line.menu_item_id,
            name_snapshot=line.menu_item_name or line.canonical.title,
            base_price_cents=0,
            option_adjustment_cents=0,
            add_on_total_cents=0,
            unit_price_cents=(line.canonical.unit_price_cents or 0),
            quantity=line.canonical.quantity,
            line_total_cents=(line.canonical.unit_price_cents or 0) * line.canonical.quantity,
            add_ons_snapshot=add_ons_snapshot or None,
            special_instructions=f"{provider}: {line.canonical.title}",
        ))

        import types as _types
        resolved_items.append(_types.SimpleNamespace(
            menu_item_id=line.menu_item_id,
            breakdown=_types.SimpleNamespace(quantity=line.canonical.quantity),
            selected_options_snapshot=[],
            add_ons_snapshot=add_ons_snapshot,
            removed_ingredients_snapshot=[],
        ))

    db.add(OrderEvent(
        order_id=order.id, business_id=business.id, old_status=None,
        new_status=OrderStatus.ACCEPTED.value,
        reason=f"Accepted via {provider} order {canonical.external_order_id}",
    ))

    if resolved_items and getattr(business, "inventory_enabled", False):
        consumption = await post_sale_consumption(
            db, business_id=business.id, order_id=order.id, resolved_items=resolved_items,
            created_by_user_id=None, occurred_at=canonical.placed_at or now,
        )
        if consumption.menu_items_without_recipe:
            logger.warning(
                "DELIVERY_ORDER_MISSING_RECIPES: business=%s order_id=%s menu_item_ids=%s",
                business.id, order.id, consumption.menu_items_without_recipe,
            )

    return order


# ── Accept-deadline watchdog (design §5, 30s cron) ───────────────────────────


async def delivery_accept_deadline_watchdog(ctx: dict) -> None:
    """
    Auto-deny anything past `accept_deadline_at` that's still open
    (RECEIVED / FETCHING / NEEDS_MAPPING / READY_TO_ACCEPT). NEVER
    auto-accepts, NEVER silently drops — always an active `deny_pos_order`
    call with a clear reason code, honest to both the customer (they get a
    real denial, not indefinite silence) and the books (no phantom sale).
    """
    settings = get_settings()
    grace = settings.DELIVERY_DEADLINE_GRACE_SECONDS
    cutoff = datetime.now(timezone.utc)

    async with ctx["session_factory"]() as db:
        try:
            rows = (
                await db.execute(
                    select(DeliveryOrder).where(
                        DeliveryOrder.status.in_(
                            ["RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT"]
                        ),
                        DeliveryOrder.accept_deadline_at.isnot(None),
                    ).with_for_update(skip_locked=True)
                )
            ).scalars().all()

            due = [
                row for row in rows
                if row.accept_deadline_at
                and (cutoff - row.accept_deadline_at).total_seconds() >= grace
            ]
            if not due:
                return

            for row in due:
                connection = (
                    await db.get(DeliveryConnection, row.delivery_connection_id)
                    if row.delivery_connection_id else None
                )
                logger.warning(
                    "DELIVERY_ACCEPT_DEADLINE_MISSED: business=%s provider=%s "
                    "external_order_id=%s status=%s deadline=%s — auto-denying.",
                    row.business_id, row.provider, row.external_order_id,
                    row.status, row.accept_deadline_at,
                )
                await _issue_deny(
                    db, row=row, connection=connection, reason_code="OTHER",
                    explanation="Accept/deny deadline missed — auto-denied by the accept-deadline watchdog.",
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("DELIVERY_ACCEPT_DEADLINE_WATCHDOG_FAILED")
