"""
Worker plane — the SLOW half of the order flow (design doc §5). Three ARQ
job entry points (`process_delivery_order`, `process_delivery_cancellation`,
`sync_delivery_order_status`, all registered in `backend/app/worker.py`)
plus the shared implementation they call, and the accept-deadline
watchdog's own logic.

GATES 4-6 LIVE HERE, NOT IN `intake.py`
--------------------------------------------
`intake.py`'s module docstring explains the split: environment match,
tenant active, entitlement, and connection status are all checked here,
FIRST, before any Get Order Details call — and any of the four failing
results in an ACTIVE `deny_pos_order` call (design §7: "a known store ...
gets an active, prompt deny_pos_order, not silence"), never a silent drop.
The environment compared is the one PROVEN by the webhook's signature
(`intake.py`), so a missing/forged `X-Environment` header can no longer
slip past this gate (BUG-0094 item 1).

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
convention. `Order.till_session_id` is never set (stays NULL), so this can
never reach till cash-up, same as every existing external-platform order.
A cancellation reverses stock through the SAME `reverse_consumption_on_cancel`
rule every other cancel path uses (only if the food was never started).

WHAT A MARKETPLACE ORDER BOOKS (Uber's own field definitions)
----------------------------------------------------------------
`subtotal_cents` = `sub_total` ("the total cost of each line item"),
`delivery_fee_cents` = `total_fee` ("fees ... that will be paid out to the
Merchant"), `total_cents` = `total` ("the entire order, including taxes and
fees that will be paid out to the Merchant excluding Marketplace Fees"). The
previous revision booked `total` as the food subtotal too, and every line at
R0 (it read a non-existent `price.amount`). Lines now carry the channel's
own prices, which can legitimately differ from the in-store menu.

AN UNMAPPED ITEM NEVER AUTO-CREATES A SALE
-----------------------------------------------
If any line/modifier fails to resolve against `external_product_mappings`,
the `DeliveryOrder` row goes to `NEEDS_MAPPING` and NOTHING is written to
`orders`/`order_items` — no Order, no stock deduction. The accept-deadline
watchdog is the only thing that ever moves a `NEEDS_MAPPING` order forward
on its own, and it always denies, never accepts.

EVERY TRANSITION IS AUDITED
-------------------------------
`_transition()` is the only place a `delivery_orders.status` changes, and it
writes an `AuditEvent` (`delivery.order.status_changed`, or the caller's own
action name for a staff action) every time — worker transitions included,
which previously left only a log line.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.delivery import registry
from backend.app.delivery.base import DeliveryOrderContext
from backend.app.delivery.canonical import (
    CanonicalDeliveryLine,
    CanonicalDeliveryModifier,
    CanonicalDeliveryOrder,
)
from backend.app.delivery.entitlements import is_provider_entitled, may_process_orders
from backend.app.delivery.errors import (
    DeliveryCapabilityUnsupported,
    DeliveryProviderError,
    DeliveryProviderNotImplemented,
    DeliveryProviderTimeout,
)
from backend.app.external_orders.adapters import order_id_prefix
from backend.app.external_orders.service import (
    EXTERNAL_PAYMENT_METHOD,
    LIVE_DISPLAY_ID_MATCH_WINDOW,
    normalise_key,
)
from backend.app.inventory.cancellation import reverse_consumption_on_cancel
from backend.app.inventory.ledger import post_sale_consumption
from shared.enums import AuditScope, ExternalMappingKind, OrderStatus, PLATFORM_TO_ORDER_SOURCE
from shared.models.audit import AuditEvent
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
_SCHEDULED_EVENT_TYPE = "orders.scheduled.notification"
_ORDER_EDIT_EVENT_TYPE = "orders.customer_order_edit"

#: Statuses the accept-deadline watchdog scans (and auto-denies past deadline).
#: Uber auto-cancels an order not accepted or denied within 11.5 minutes.
_UBER_ACCEPT_WINDOW = timedelta(seconds=690)

OPEN_STATUSES = ("RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT")
#: A real Order exists and the kitchen has it.
ACCEPTED_STATUSES = ("ACCEPTED", "ACCEPT_UNCONFIRMED")
#: Kitchen statuses after which the food has left the building.
_HANDED_OVER_STATUSES = frozenset({
    OrderStatus.COLLECTED.value, OrderStatus.DELIVERED.value, OrderStatus.COMPLETED.value,
})

#: Inline retries for an ambiguous accept failure (timeout / 5xx / transport):
#: total wall time ~2s, well inside the provider's accept window.
_ACCEPT_RETRY_DELAYS_SECONDS = (0.5, 1.5)

#: A provider display code is short and reused; a CSV/report row carrying
#: one only counts as the same sale inside this window (one shared rule,
#: applied in both directions — see external_orders/service.py).
DISPLAY_ID_MATCH_WINDOW = LIVE_DISPLAY_ID_MATCH_WINDOW

_PROVIDER_LABELS = {"UBER_EATS": "Uber Eats", "MR_D": "Mr D"}


# ── ARQ job entry points (registered in backend/app/worker.py) ──────────────


async def process_delivery_order(
    ctx: dict, provider: str, business_id: str, external_order_id: str,
    event_type: str | None = None, force_accept: bool = False,
) -> None:
    """
    ARQ job. Opens its own session — no FastAPI dependency injection. Never
    raises for a business-logic outcome (misconfiguration, unmapped items,
    provider error) — only an infrastructure failure propagates, so ARQ's
    own retry (`max_tries`) applies to THAT, not to a normal denial.

    `force_accept` (default False, backward-compatible with the webhook
    plane's job_args, which never pass it): set only by
    `routes_delivery.py::accept_order` when staff explicitly confirm a
    READY_TO_ACCEPT order (the `auto_accept_enabled=False` path, migration
    0046) — bypasses the auto-accept gate for THIS one already-resolved
    order without touching the connection's own setting.
    """
    async with ctx["session_factory"]() as db:
        try:
            await _handle_order(
                db, provider=provider, business_id=uuid.UUID(business_id),
                external_order_id=external_order_id, event_type=event_type,
                force_accept=force_accept,
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
    ARQ job for `orders.cancel` / `orders.failure`. Marks the
    `delivery_orders` row CANCELLED and, if a real `Order` exists that has
    not been handed over yet, cancels it through the same status/OrderEvent
    path staff use, reversing stock by the platform-wide rule (only if the
    kitchen never started it — `inventory/cancellation.py`).
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


async def sync_delivery_order_status(
    ctx: dict, business_id: str, order_id: str, new_status: str, reason: str | None = None,
) -> None:
    """
    ARQ job, enqueued by `routes_orders.py::update_order_status` after a
    staff status change on a marketplace order. Pushes the change to the
    provider where it means something there:

      READY      -> "order ready for pickup" (capability `ready_signal`)
      CANCELLED  -> restaurant-initiated cancel (capability `restaurant_cancel`)
      COLLECTED / DELIVERED / COMPLETED -> local FULFILLED only, no call

    `business_id` comes from the staff member's JWT; the lookup below is
    scoped by it, so a job can only ever touch that tenant's row. A failed
    push never blocks or reverses the kitchen's own status change — it is
    recorded on the row (`last_provider_sync_error`) and raised to managers.
    """
    async with ctx["session_factory"]() as db:
        try:
            await _sync_order_status(
                db, business_id=uuid.UUID(business_id), order_id=uuid.UUID(order_id),
                new_status=new_status, reason=reason,
            )
        except Exception:
            await db.rollback()
            logger.exception(
                "DELIVERY_STATUS_SYNC_FAILED: business_id=%s order_id=%s new_status=%s",
                business_id, order_id, new_status,
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


def _transition(
    db: AsyncSession,
    row: DeliveryOrder,
    new_status: str,
    *,
    reason: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str = "delivery.order.status_changed",
    extra: dict | None = None,
) -> None:
    """Change a delivery order's status and audit it. The only writer of `status`."""
    old_status = row.status
    row.status = new_status
    diff = {
        "provider": row.provider,
        "external_order_id": row.external_order_id,
        "from": old_status,
        "to": new_status,
    }
    if reason:
        diff["reason"] = reason
    if extra:
        diff.update(extra)
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=row.business_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type="delivery_order",
        target_id=row.id,
        diff_json=diff,
    ))


def _audit(
    db: AsyncSession, row: DeliveryOrder, action: str, *,
    actor_user_id: uuid.UUID | None = None, extra: dict | None = None,
) -> None:
    """Audit a non-status event on a delivery order (provider push, edit notice)."""
    diff = {"provider": row.provider, "external_order_id": row.external_order_id}
    if extra:
        diff.update(extra)
    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=row.business_id,
        actor_user_id=actor_user_id, action=action, target_type="delivery_order",
        target_id=row.id, diff_json=diff,
    ))


def _context_for(
    row: DeliveryOrder, connection: DeliveryConnection | None, *, environment: str | None = None,
) -> DeliveryOrderContext:
    return DeliveryOrderContext(
        business_id=row.business_id,
        connection_id=connection.id if connection else uuid.uuid4(),
        external_store_id=row.external_store_id,
        environment=environment or (connection.environment if connection else "production"),
    )


def _note_auth_failure(connection: DeliveryConnection | None, status_code: int | None) -> None:
    """
    401/403 after the adapter already re-minted its token once means the
    credentials, scopes or order-manager nomination are wrong — a
    configuration problem every later order will hit too. Surface it on the
    connection (dashboard: "Attention required") instead of only in logs.
    """
    if connection is None or status_code not in (401, 403):
        return
    if connection.status in ("CONNECTED", "SANDBOX", "AWAITING_APPROVAL"):
        connection.status = "ATTENTION_REQUIRED"
    connection.error_reason = (
        f"The provider rejected NextGen's credentials (HTTP {status_code}). Check the app "
        "credentials/scopes and that NextGen is this store's nominated order manager."
    )


async def _issue_deny(
    db: AsyncSession,
    *,
    row: DeliveryOrder,
    connection: DeliveryConnection | None,
    reason_code: str,
    explanation: str,
    out_of_stock_items: list[str] | None = None,
    environment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str = "delivery.order.status_changed",
) -> None:
    adapter = registry.get_provider(row.provider)
    context = _context_for(row, connection, environment=environment)
    try:
        await adapter.deny_order(
            external_order_id=row.external_order_id, context=context,
            reason_code=reason_code, explanation=explanation,
            out_of_stock_items=out_of_stock_items,
        )
    except (DeliveryProviderNotImplemented, DeliveryProviderError, DeliveryProviderTimeout) as exc:
        _note_auth_failure(connection, getattr(exc, "status_code", None))
        logger.exception(
            "DELIVERY_DENY_CALL_FAILED: business=%s external_order_id=%s reason=%s — "
            "marked DENIED locally regardless; Uber's own 11.5-minute timeout is the backstop.",
            row.business_id, row.external_order_id, reason_code,
        )
    _transition(
        db, row, "DENIED", reason=f"{reason_code}: {explanation}",
        actor_user_id=actor_user_id, action=action,
    )
    row.denied_at = datetime.now(timezone.utc)
    row.deny_reason_code = reason_code
    row.deny_reason_text = explanation


async def _handle_order(
    db: AsyncSession, *, provider: str, business_id: uuid.UUID, external_order_id: str,
    event_type: str | None, force_accept: bool = False,
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

    if event_type == _ORDER_EDIT_EVENT_TYPE and row.status != "RECEIVED":
        await _handle_order_edit(db, row=row)
        return

    if row.status in ACCEPTED_STATUSES and event_type == "orders.notification":
        # Uber: a scheduled order "must be accepted twice: within 10 minutes
        # of orders.scheduled.notification and again within 10 minutes of
        # orders.notification". The Order already exists from the first
        # accept — only the provider handshake is repeated, never the sale.
        if await _first_event_type(db, row) == _SCHEDULED_EVENT_TYPE:
            await _reaccept_scheduled(db, row=row)
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
        row.failure_reason = misconfig
        logger.warning(
            "DELIVERY_ORDER_MISCONFIGURED: business=%s external_order_id=%s reason=%s — "
            "issuing an active deny (never silence for a known store).",
            business_id, external_order_id, misconfig,
        )
        # Deny through the environment the order provably came from (an
        # ENV_MISMATCH order would otherwise be denied with the other app).
        await _issue_deny(
            db, row=row, connection=connection, reason_code=_MISCONFIG_DENY_CODE,
            explanation=f"NextGen configuration issue: {misconfig}",
            environment=webhook_event.environment if webhook_event and webhook_event.environment else None,
        )
        await db.commit()
        await _publish_alert(business_id, "delivery_order_denied", row, external_order_id)
        return

    adapter = registry.get_provider(provider)
    context = _context_for(row, connection)

    _transition(db, row, "FETCHING")
    await db.flush()

    try:
        canonical = await adapter.fetch_order(external_order_id=external_order_id, context=context)
    except DeliveryProviderTimeout:
        # Transient — revert to RECEIVED so a retried job re-enters cleanly,
        # and re-raise so ARQ's max_tries actually retries it.
        _transition(db, row, "RECEIVED", reason="fetch timed out — will retry")
        await db.commit()
        raise
    except DeliveryProviderError as exc:
        # BUG-0091 fix: a known store's order must never sit in a dead
        # terminal state — "a known store gets an active deny, never silence".
        row.failure_reason = f"fetch_order failed: {exc}"
        _note_auth_failure(connection, exc.status_code)
        logger.exception("DELIVERY_ORDER_FETCH_FAILED: business=%s external_order_id=%s", business_id, external_order_id)
        await _issue_deny(
            db, row=row, connection=connection, reason_code="POS_NOT_READY",
            explanation=f"NextGen could not fetch this order's details: {exc}",
        )
        await db.commit()
        await _publish_alert(business_id, "delivery_order_denied", row, external_order_id)
        return

    _apply_fetched(row, canonical)
    first_event_type = webhook_event.event_type if webhook_event else event_type
    if first_event_type != _SCHEDULED_EVENT_TYPE:
        _tighten_accept_deadline(row, canonical.placed_at)

    # BUG-0092 fix, re-based on the food sale: refuse to book a sale the
    # platform could not price. A wrong number in the books is worse than a
    # declined order, so an unpriceable order is actively denied — no food
    # value, or a provider-reported total of zero/below (zero is never a
    # genuine price for a real paid order).
    food_cents = _food_sale_cents(canonical)
    if (
        food_cents is None or food_cents <= 0
        or (canonical.total_cents is not None and canonical.total_cents <= 0)
    ):
        row.failure_reason = (
            f"Order could not be priced (sub_total={canonical.subtotal_cents!r}, "
            f"total={canonical.total_cents!r}) — refusing to book a sale the platform cannot price."
        )
        logger.warning(
            "DELIVERY_ORDER_UNPRICEABLE: business=%s external_order_id=%s — "
            "denying rather than booking a zero/unparseable-value sale.",
            business_id, external_order_id,
        )
        await _issue_deny(
            db, row=row, connection=connection, reason_code="PRICING",
            explanation="NextGen could not determine this order's value from the provider response.",
        )
        await db.commit()
        await _publish_alert(business_id, "delivery_order_denied", row, external_order_id)
        return

    existing = await _existing_sale(db, business=business, provider=provider, canonical=canonical)
    if existing is not None:
        # Never book the same sale twice (e.g. a provider report for this
        # order was imported first). No provider call: the sale already
        # stands, so neither accepting nor denying it here would be honest.
        row.failure_reason = (
            f"A sale for this order is already recorded as {existing.order_number} — "
            "not booked a second time."
        )
        _transition(db, row, "FAILED", reason=row.failure_reason)
        await db.commit()
        logger.warning(
            "DELIVERY_ORDER_ALREADY_BOOKED: business=%s external_order_id=%s existing_order=%s",
            business_id, external_order_id, existing.id,
        )
        await _publish_alert(business_id, "delivery_order_attention", row, external_order_id)
        return

    resolution = await _resolve_delivery_lines(
        db, business_id=business_id, provider=provider, canonical=canonical
    )
    if resolution.blocks_import:
        row.unresolved_items_json = resolution.unresolved_entries
        _transition(db, row, "NEEDS_MAPPING", extra={"unresolved": len(resolution.unresolved_entries)})
        await db.commit()
        logger.warning(
            "DELIVERY_ORDER_NEEDS_MAPPING: business=%s external_order_id=%s unresolved=%s — "
            "never auto-created a sale; surfaced for staff, will auto-deny at deadline if unresolved.",
            business_id, external_order_id, resolution.unresolved_names,
        )
        await _publish_alert(business_id, "delivery_needs_mapping", row, external_order_id)
        return

    if resolution.unavailable:
        # Design §6: item-level 86ing is NextGen-internal (no confirmed
        # per-item availability push — see uber/adapter.py), so an order for
        # an item the restaurant marked unavailable is denied with the
        # provider's own ITEM_AVAILABILITY code rather than accepted into a
        # kitchen that cannot make it.
        titles = [u["external_text"] for u in resolution.unavailable]
        row.failure_reason = "Unavailable: " + ", ".join(titles)
        await _issue_deny(
            db, row=row, connection=connection, reason_code="ITEM_AVAILABILITY",
            explanation="Not available right now: " + ", ".join(titles),
            out_of_stock_items=[u["external_id"] for u in resolution.unavailable if u.get("external_id")],
        )
        await db.commit()
        await _publish_alert(business_id, "delivery_order_denied", row, external_order_id)
        return

    # Design gap fix (migration 0046): `auto_accept_enabled` defaults FALSE —
    # a fully-mapped order stops at READY_TO_ACCEPT and waits for an explicit
    # staff accept, which re-enqueues this SAME job with force_accept=True.
    if not force_accept and not getattr(connection, "auto_accept_enabled", False):
        _transition(db, row, "READY_TO_ACCEPT")
        await db.commit()
        logger.info(
            "DELIVERY_ORDER_READY_TO_ACCEPT: business=%s external_order_id=%s — fully mapped, "
            "auto_accept_enabled=False, awaiting explicit staff accept.",
            business_id, external_order_id,
        )
        await _publish_alert(business_id, "delivery_ready_to_accept", row, external_order_id)
        return

    order = await _create_order_from_canonical(
        db, business=business, provider=provider, canonical=canonical, resolution=resolution,
    )
    row.order_id = order.id
    await db.flush()

    outcome, error, status_code = await _accept_with_retries(
        adapter, external_order_id=external_order_id, context=context, our_order_id=str(order.id),
    )

    if outcome == "rejected":
        # The provider definitively refused the accept (e.g. the order was
        # already cancelled/expired). Nothing has been committed or shown to
        # the kitchen yet, so the honest outcome is NO sale: roll the Order,
        # its items and its stock movements back together. Every ORM object
        # is expired by the rollback, so the connection id is read FIRST.
        connection_id = connection.id if connection else None
        await db.rollback()
        await _record_rejected_accept(
            db, provider=provider, business_id=business_id,
            external_order_id=external_order_id, canonical=canonical, error=error,
            status_code=status_code, connection_id=connection_id,
        )
        return

    if outcome == "accepted":
        _transition(db, row, "ACCEPTED")
        row.accepted_at = datetime.now(timezone.utc)
    else:
        # Timed out / 5xx / transport error even after retries: the provider
        # MAY have accepted. The kitchen gets the order (the customer most
        # likely expects food), and the row is parked in a status the
        # watchdog never denies, for a human to confirm on the provider side.
        row.failure_reason = (
            "The provider did not confirm our accept "
            f"({error}). Check the order on the provider's tablet/portal, then use "
            "'Retry accept' or 'Mark accepted'."
        )
        _transition(db, row, "ACCEPT_UNCONFIRMED", reason=error)
        logger.error(
            "DELIVERY_ACCEPT_UNCONFIRMED: business=%s external_order_id=%s order_id=%s error=%s",
            business_id, external_order_id, order.id, error,
        )

    await db.commit()
    await _publish_alert(business_id, "new_order", row, external_order_id, order=order)
    if row.status == "ACCEPT_UNCONFIRMED":
        await _publish_alert(business_id, "delivery_order_attention", row, external_order_id, order=order)


async def _accept_with_retries(
    adapter, *, external_order_id: str, context: DeliveryOrderContext, our_order_id: str,
) -> tuple[str, str | None, int | None]:
    """
    (outcome, why, http_status) with outcome "accepted" | "rejected" | "unconfirmed".

    A 4xx (other than 408/429) is a definitive "no" from the provider. A
    timeout, 5xx, 408/429 or transport error is ambiguous — the accept may
    have landed — so it is retried briefly and, if still unanswered,
    reported as unconfirmed rather than guessed either way.
    """
    last_error: str | None = None
    last_status: int | None = None
    for delay in (0.0, *_ACCEPT_RETRY_DELAYS_SECONDS):
        if delay:
            await asyncio.sleep(delay)
        try:
            await adapter.accept_order(
                external_order_id=external_order_id, context=context, our_order_id=our_order_id,
            )
            return "accepted", None, None
        except DeliveryProviderNotImplemented as exc:
            return "rejected", str(exc), None
        except DeliveryProviderError as exc:
            code = exc.status_code
            if code is not None and 400 <= code < 500 and code not in (408, 429):
                return "rejected", str(exc), code
            last_error, last_status = str(exc), code
        except DeliveryProviderTimeout as exc:
            last_error, last_status = f"timed out: {exc}", None
    return "unconfirmed", last_error, last_status


async def _record_rejected_accept(
    db: AsyncSession, *, provider: str, business_id: uuid.UUID, external_order_id: str,
    canonical: CanonicalDeliveryOrder, error: str | None, status_code: int | None,
    connection_id: uuid.UUID | None,
) -> None:
    """Re-lock after the rollback and record the refused accept (no sale exists)."""
    row = await _load_locked_row(
        db, business_id=business_id, provider=provider, external_order_id=external_order_id
    )
    if row is None or row.status != "RECEIVED":
        await db.commit()
        return
    connection = await db.get(DeliveryConnection, connection_id) if connection_id else None
    _note_auth_failure(connection, status_code)
    _apply_fetched(row, canonical)
    row.failure_reason = f"The provider refused our accept — no sale was recorded ({error})."
    _transition(db, row, "FAILED", reason=row.failure_reason)
    await db.commit()
    logger.error(
        "DELIVERY_ACCEPT_REJECTED: business=%s external_order_id=%s error=%s — rolled back, no sale.",
        business_id, external_order_id, error,
    )
    await _publish_alert(business_id, "delivery_order_attention", row, external_order_id)


async def _first_event_type(db: AsyncSession, row: DeliveryOrder) -> str | None:
    if not row.raw_event_id:
        return None
    event = await db.get(DeliveryWebhookEvent, row.raw_event_id)
    return event.event_type if event else None


async def _reaccept_scheduled(db: AsyncSession, *, row: DeliveryOrder) -> None:
    # Uber may retry the orders.notification under a NEW event_id, which slips
    # past intake's event_id dedupe. The second handshake is owed once per
    # order, so the audit trail decides — never a repeat accept call.
    already = (await db.execute(
        select(AuditEvent.id).where(
            AuditEvent.business_id == row.business_id,
            AuditEvent.target_type == "delivery_order",
            AuditEvent.target_id == row.id,
            AuditEvent.action == "delivery.order.scheduled_reaccepted",
        ).limit(1)
    )).scalar_one_or_none()
    if already is not None:
        logger.info("DELIVERY_SCHEDULED_REACCEPT_ALREADY_DONE: id=%s — idempotent no-op.", row.id)
        return
    connection = await db.get(DeliveryConnection, row.delivery_connection_id) if row.delivery_connection_id else None
    adapter = registry.get_provider(row.provider)
    outcome, error, status_code = await _accept_with_retries(
        adapter, external_order_id=row.external_order_id, context=_context_for(row, connection),
        our_order_id=str(row.order_id),
    )
    _note_auth_failure(connection, status_code)
    now = datetime.now(timezone.utc)
    row.last_provider_sync_at = now
    if outcome == "accepted":
        row.last_provider_sync_error = None
        _audit(db, row, "delivery.order.scheduled_reaccepted")
    else:
        row.last_provider_sync_error = f"scheduled re-accept {outcome}: {error}"
        _audit(db, row, "delivery.order.scheduled_reaccept_failed", extra={"error": error})
    await db.commit()
    if outcome != "accepted":
        await _publish_alert(row.business_id, "delivery_order_attention", row, row.external_order_id)


async def _handle_order_edit(db: AsyncSession, *, row: DeliveryOrder) -> None:
    """
    `orders.customer_order_edit`. Before acceptance the fetched snapshot is
    simply stale, so the order is re-fetched from scratch. After acceptance
    the kitchen already has it: NextGen does not rewrite a sale it cannot
    see the new contents of, it flags it for a human instead.
    """
    if row.status in ("NEEDS_MAPPING", "READY_TO_ACCEPT"):
        _transition(db, row, "RECEIVED", reason="customer edited the order — re-fetching")
        row.unresolved_items_json = None
        await db.commit()
        await _handle_order(
            db, provider=row.provider, business_id=row.business_id,
            external_order_id=row.external_order_id, event_type=None,
        )
        return
    _audit(db, row, "delivery.order.customer_edited", extra={"status": row.status})
    if row.status in ACCEPTED_STATUSES:
        row.last_provider_sync_error = (
            "The customer edited this order on the provider after it was accepted — "
            "check the provider's tablet; NextGen's copy shows the original."
        )
    await db.commit()
    await _publish_alert(row.business_id, "delivery_order_attention", row, row.external_order_id)


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

    order = await db.get(Order, row.order_id) if row.order_id else None
    now = datetime.now(timezone.utc)
    row.cancelled_at = now

    if order is not None and order.status in _HANDED_OVER_STATUSES:
        # The food already left the building. Uber's cancel is a financial
        # event between Uber and the customer; the sale and the stock it used
        # stand as recorded — the payout report settles what was paid.
        _transition(
            db, row, "CANCELLED",
            reason=f"{provider} cancelled after hand-over ({order.status}) — sale left as recorded",
        )
        await db.commit()
        await _publish_alert(business_id, "delivery_order_attention", row, external_order_id, order=order)
        return

    _transition(db, row, "CANCELLED", reason=f"cancelled by {provider}")

    old_status = None
    if order is not None and order.status != OrderStatus.CANCELLED.value:
        old_status = order.status
        order.status = OrderStatus.CANCELLED.value
        order.cancelled_reason = f"Cancelled by {_PROVIDER_LABELS.get(provider, provider)} ({external_order_id})"
        db.add(OrderEvent(
            order_id=order.id, business_id=business_id, old_status=old_status,
            new_status=OrderStatus.CANCELLED.value,
            reason="Delivery marketplace reported this order cancelled.",
        ))
        business = await db.get(Business, business_id)
        # Same rule as every other cancel path: stock comes back only if the
        # kitchen never started the order.
        await reverse_consumption_on_cancel(
            db,
            business_id=business_id,
            inventory_enabled=bool(getattr(business, "inventory_enabled", False)),
            order_id=order.id,
            previous_status=old_status,
            created_by_user_id=None,
        )

    await db.commit()
    logger.warning(
        "DELIVERY_ORDER_CANCELLED: business=%s external_order_id=%s order_id=%s",
        business_id, external_order_id, row.order_id,
    )
    if order is not None and old_status is not None:
        await _publish_order_cancelled(business_id, row, order, old_status)
    else:
        await _publish_alert(business_id, "delivery_order_cancelled", row, external_order_id)


async def _sync_order_status(
    db: AsyncSession, *, business_id: uuid.UUID, order_id: uuid.UUID, new_status: str,
    reason: str | None,
) -> None:
    row = (
        await db.execute(
            select(DeliveryOrder)
            .where(DeliveryOrder.business_id == business_id, DeliveryOrder.order_id == order_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return  # not a live-API order (e.g. a CSV import) — nothing to push

    if new_status in _HANDED_OVER_STATUSES:
        if row.status in ACCEPTED_STATUSES:
            _transition(db, row, "FULFILLED", reason=f"kitchen marked {new_status}")
            await db.commit()
        return

    if new_status not in (OrderStatus.READY.value, OrderStatus.CANCELLED.value):
        return
    if row.status not in ACCEPTED_STATUSES:
        return

    connection = await db.get(DeliveryConnection, row.delivery_connection_id) if row.delivery_connection_id else None
    adapter = registry.get_provider(row.provider)
    context = _context_for(row, connection)
    now = datetime.now(timezone.utc)
    error: str | None = None
    try:
        if new_status == OrderStatus.READY.value:
            await adapter.mark_order_ready(external_order_id=row.external_order_id, context=context)
        else:
            await adapter.cancel_order(
                external_order_id=row.external_order_id, context=context,
                reason_code="OTHER", explanation=reason or "Cancelled by the restaurant",
            )
    except DeliveryCapabilityUnsupported:
        error = f"{_PROVIDER_LABELS.get(row.provider, row.provider)} does not support this update — update it on the provider's own tablet."
    except (DeliveryProviderError, DeliveryProviderTimeout, DeliveryProviderNotImplemented) as exc:
        _note_auth_failure(connection, getattr(exc, "status_code", None))
        error = str(exc)

    row.last_provider_sync_at = now
    if error is None:
        row.last_provider_sync_error = None
        if new_status == OrderStatus.CANCELLED.value:
            row.cancelled_at = now
            _transition(db, row, "CANCELLED", reason="cancelled by the restaurant; provider notified")
        else:
            _audit(db, row, "delivery.order.ready_signalled")
    else:
        verb = "mark ready" if new_status == OrderStatus.READY.value else "cancel"
        row.last_provider_sync_error = f"Could not {verb} on the provider: {error}"
        _audit(db, row, "delivery.order.provider_sync_failed", extra={"status": new_status, "error": error})
    await db.commit()
    if error is not None:
        await _publish_alert(business_id, "delivery_order_attention", row, row.external_order_id)


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
    if not may_process_orders(connection):
        return "CONNECTION_NOT_LIVE"
    return None


def _food_sale_cents(canonical: CanonicalDeliveryOrder) -> int | None:
    """The food sale: `sub_total`, else the sum of line totals, else nothing."""
    if canonical.subtotal_cents is not None:
        return canonical.subtotal_cents
    totals = [
        line.total_price_cents if line.total_price_cents is not None
        else (line.unit_price_cents * line.quantity if line.unit_price_cents is not None else None)
        for line in canonical.lines
    ]
    if totals and all(t is not None for t in totals):
        return sum(totals)
    return None


def _tighten_accept_deadline(row: DeliveryOrder, placed_at: datetime | None) -> None:
    """
    BUG-0185. Uber's accept/deny window runs from when the customer placed the
    order, but intake can only start the clock when the webhook reaches us — and
    a delivery Uber had to retry (10s, then 30/60/120s...) arrives late. The
    webhook's own `event_time` can't anchor it: Uber confirmed (2026-10-29) it is
    the time of that notification attempt and may differ between retries.
    `placed_at` from the order details can. Only ever shortens the deadline; a
    `placed_at` in the future (clock skew) is ignored. Not for scheduled orders,
    whose window starts at the scheduled notification, not at placement — and
    not for anything placed longer ago than Uber's whole window: an on-demand
    order that old is already cancelled by Uber, so such an order is far more
    likely a scheduled one whose first notification never reached us, and
    tightening would deny it at once (security review, 2026-10-29).
    """
    if placed_at is None or row.accept_deadline_at is None:
        return
    age = datetime.now(timezone.utc) - placed_at
    if age < timedelta(0) or age > _UBER_ACCEPT_WINDOW:
        return
    from_placement = placed_at + timedelta(seconds=get_settings().DELIVERY_ACCEPT_DEADLINE_SECONDS)
    if from_placement < row.accept_deadline_at:
        row.accept_deadline_at = from_placement


def _apply_fetched(row: DeliveryOrder, canonical: CanonicalDeliveryOrder) -> None:
    row.display_id = canonical.display_id
    row.placed_at = canonical.placed_at
    row.gross_cents = _food_sale_cents(canonical)
    row.fee_cents = canonical.fee_cents
    row.currency = canonical.currency or row.currency
    row.fetched_order_snapshot_json = _build_snapshot(canonical)


def _build_snapshot(canonical: CanonicalDeliveryOrder) -> dict:
    """
    Allow-listed snapshot — never the raw provider response (see canonical.py).
    Line titles/quantities/modifiers are here so staff can judge an order
    before accepting it; no phone number, no address.
    """
    return {
        "display_id": canonical.display_id,
        "order_type": canonical.order_type,
        "item_count": canonical.item_count,
        "currency": canonical.currency,
        "subtotal_cents": canonical.subtotal_cents,
        "tax_cents": canonical.tax_cents,
        "fee_cents": canonical.fee_cents,
        "total_cents": canonical.total_cents,
        "cash_amount_due_cents": canonical.cash_amount_due_cents,
        "ready_for_pickup_at": (
            canonical.ready_for_pickup_at.isoformat() if canonical.ready_for_pickup_at else None
        ),
        "special_instructions": canonical.special_instructions,
        "customer_first_name": canonical.customer_first_name,
        "fulfillment_issues": list(canonical.fulfillment_issues),
        "lines": [
            {
                "external_id": line.external_item_id,
                "title": line.title,
                "quantity": line.quantity,
                "unit_price_cents": line.unit_price_cents,
                "total_price_cents": line.total_price_cents,
                "special_instructions": line.special_instructions,
                "modifiers": [
                    {
                        "external_id": m.external_id,
                        "title": m.title,
                        "quantity": m.quantity,
                        "removed": m.removed,
                        "group_title": m.group_title,
                    }
                    for m in line.modifiers
                ],
            }
            for line in canonical.lines
        ],
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
                "delivery_status": row.status,
                "provider": row.provider,
                "external_order_id": external_order_id,
                "display_id": row.display_id,
                "order_id": str(order.id) if order else None,
                "order_number": order.order_number if order else None,
            }),
        )
    except Exception:
        logger.warning("DELIVERY_ALERT_PUBLISH_FAILED: business=%s type=%s", business_id, alert_type)

    try:
        from backend.app.push_service import send_business_notification, send_order_alert
        label = _PROVIDER_LABELS.get(row.provider, row.provider)
        code = row.display_id or external_order_id
        if alert_type in ("new_order", "delivery_ready_to_accept", "delivery_needs_mapping"):
            await send_order_alert(business_id, (order.order_number if order else f"{label} {code}"), "DELIVERY")
        elif alert_type in ("delivery_order_attention", "delivery_order_cancelled"):
            await send_business_notification(
                business_id, title=f"{label} order {code} needs attention",
                body=row.failure_reason or row.last_provider_sync_error or row.status,
            )
    except Exception:
        logger.warning("DELIVERY_ALERT_PUSH_FAILED: business=%s type=%s", business_id, alert_type)


async def _publish_order_cancelled(
    business_id: uuid.UUID, row: DeliveryOrder, order: Order, old_status: str,
) -> None:
    """
    Same `order_status_changed` event shape `routes_orders.py` publishes for
    a staff status change, so the kitchen board drops the order exactly as
    it would for an in-store cancel — plus a push, since food may be cooking.
    """
    import json

    try:
        from backend.app.db.session import get_redis
        redis = await get_redis()
        await redis.publish(
            f"orders:{business_id}",
            json.dumps({
                "type": "order_status_changed",
                "order_id": str(order.id),
                "order_number": order.order_number,
                "old_status": old_status,
                "new_status": OrderStatus.CANCELLED.value,
                "total_cents": order.total_cents,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "provider": row.provider,
            }),
        )
    except Exception:
        logger.warning("DELIVERY_CANCEL_PUBLISH_FAILED: business=%s order=%s", business_id, order.id)

    try:
        from backend.app.push_service import send_business_notification
        label = _PROVIDER_LABELS.get(row.provider, row.provider)
        await send_business_notification(
            business_id, title=f"{label} order {order.order_number} CANCELLED",
            body="The customer's order was cancelled on the delivery platform — stop preparing it.",
        )
    except Exception:
        logger.warning("DELIVERY_CANCEL_PUSH_FAILED: business=%s order=%s", business_id, order.id)


# ── Mapping resolution (external_id-keyed — see design §6) ──────────────────


@dataclass
class _ResolvedDeliveryLine:
    canonical: CanonicalDeliveryLine
    menu_item_id: uuid.UUID | None = None
    menu_item_name: str | None = None
    #: [{group_id, group_name, option_id, option_name, price_delta_cents}] — ledger shape
    options: list[dict] = field(default_factory=list)
    #: [(MenuAddOn, CanonicalDeliveryModifier)]
    add_ons: list[tuple] = field(default_factory=list)
    #: [{id, name}] — ledger shape
    removed: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    unavailable: list[dict] = field(default_factory=list)
    is_ignored: bool = False

    @property
    def add_on_ids(self) -> list[uuid.UUID]:
        return [a.id for a, _ in self.add_ons]

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
    def unresolved_entries(self) -> list[dict]:
        """
        What the mapping screen needs to close the loop: the provider's own
        id (THE mapping key), its text, and whether it is an ITEM or a
        MODIFIER. The manager dashboard's "Map this item" deep link reads
        `external_id`/`external_text` — a bare name string (the previous
        shape) left it with no id to map and showed "Unknown item".
        """
        entries: list[dict] = []
        for line in self.lines:
            if not line.is_resolved:
                entries.append({
                    "kind": ExternalMappingKind.ITEM.value,
                    "external_id": line.canonical.external_item_id,
                    "external_text": line.canonical.title,
                })
            entries.extend(line.unresolved)
        return entries

    @property
    def unresolved_names(self) -> list[str]:
        return [e["external_text"] for e in self.unresolved_entries]

    @property
    def unavailable(self) -> list[dict]:
        return [u for line in self.lines for u in line.unavailable]


def _option_groups(menu_item: MenuItem) -> list[dict]:
    return list(((menu_item.options_json or {}).get("option_groups")) or [])


def _option_by_id(menu_item: MenuItem, option_id: str) -> dict | None:
    for group in _option_groups(menu_item):
        for option in group.get("options") or []:
            if str(option.get("id")) == str(option_id):
                return {
                    "group_id": str(group.get("id")),
                    "group_name": group.get("name"),
                    "option_id": str(option.get("id")),
                    "option_name": option.get("name"),
                    "price_delta_cents": int(option.get("price_delta_cents") or 0),
                    "_enabled": option.get("is_enabled", True) is not False
                                and group.get("is_enabled", True) is not False,
                }
    return None


def _option_by_name(menu_item: MenuItem, title: str) -> dict | None:
    key = normalise_key(title)
    for group in _option_groups(menu_item):
        for option in group.get("options") or []:
            if option.get("id") and normalise_key(option.get("name", "")) == key:
                return _option_by_id(menu_item, str(option["id"]))
    return None


def _removable(menu_item: MenuItem, *, ref_id: str | None = None, title: str | None = None) -> dict | None:
    removables = (menu_item.options_json or {}).get("removable_ingredients") or []
    for r in removables:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if ref_id is not None and str(r["id"]) == str(ref_id):
            return {"id": str(r["id"]), "name": r.get("name") or ""}
        if title is not None and normalise_key(r.get("name", "")) == normalise_key(title):
            return {"id": str(r["id"]), "name": r.get("name") or ""}
    return None


def _unresolved_modifier(line: CanonicalDeliveryLine, modifier: CanonicalDeliveryModifier, why: str | None = None) -> dict:
    entry = {
        "kind": ExternalMappingKind.MODIFIER.value,
        "external_id": modifier.external_id,
        "external_text": modifier.title,
        "parent_external_text": line.title,
        "group_title": modifier.group_title,
        "removed": modifier.removed,
    }
    if why:
        entry["reason"] = why
    return entry


async def _resolve_delivery_lines(
    db: AsyncSession, *, business_id: uuid.UUID, provider: str, canonical: CanonicalDeliveryOrder,
) -> _ResolutionResult:
    """
    Resolve each canonical line against `external_product_mappings`, keyed
    by the provider's STABLE `external_id` (migration 0043) — not the
    normalised-TEXT key the CSV path uses, since a live API order carries a
    real id rather than only free text. Auto-match is otherwise exactly as
    conservative as the CSV path (design §6): an exact saved mapping, or an
    exact menu-item/add-on/option NAME match. Nothing fuzzier.

    A modifier mapping may point at an add-on, a size/option on the item,
    or a removable ingredient — the same three targets the CSV importer
    honours (`external_orders/service.py::resolve_orders`). Previously only
    add-on targets resolved here, so an option/removal mapping a manager had
    saved still left the order stuck in NEEDS_MAPPING until auto-deny.
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
        menu_item: MenuItem | None = None

        if mapping and mapping.is_ignored:
            resolved.is_ignored = True
        elif mapping and mapping.menu_item_id in item_by_id:
            menu_item = item_by_id[mapping.menu_item_id]
            if mapping.option_id:
                option = _option_by_id(menu_item, mapping.option_id)
                if option is None:
                    menu_item = None  # mapping points at an option that no longer exists
                else:
                    resolved.options.append(option)
        elif normalise_key(line.title) in item_by_name:
            menu_item = item_by_name[normalise_key(line.title)]

        if menu_item is not None:
            resolved.menu_item_id = menu_item.id
            resolved.menu_item_name = menu_item.name
            if menu_item.is_active is False:  # NOT NULL column; only an explicit False is "86d"
                resolved.unavailable.append(
                    {"external_id": line.external_item_id, "external_text": line.title}
                )

        for modifier in line.modifiers:
            mod_mapping = (
                modifier_by_external_id.get(modifier.external_id) if modifier.external_id else None
            )
            if mod_mapping and mod_mapping.is_ignored:
                continue

            if modifier.removed:
                target = None
                if menu_item is not None and mod_mapping and mod_mapping.removable_ref_id:
                    target = _removable(menu_item, ref_id=mod_mapping.removable_ref_id)
                if target is None and menu_item is not None:
                    target = _removable(menu_item, title=modifier.title)
                if target is not None:
                    resolved.removed.append(target)
                elif not resolved.is_ignored:
                    resolved.unresolved.append(_unresolved_modifier(line, modifier, "removed ingredient"))
                continue

            if mod_mapping:
                if mod_mapping.menu_add_on_id in add_on_by_id:
                    add_on = add_on_by_id[mod_mapping.menu_add_on_id]
                    resolved.add_ons.append((add_on, modifier))
                    if add_on.is_active is False:
                        resolved.unavailable.append(
                            {"external_id": modifier.external_id, "external_text": modifier.title}
                        )
                    continue
                if mod_mapping.option_id and menu_item is not None:
                    option = _option_by_id(menu_item, mod_mapping.option_id)
                    if option is not None:
                        resolved.options.append(option)
                        if not option["_enabled"]:
                            resolved.unavailable.append(
                                {"external_id": modifier.external_id, "external_text": modifier.title}
                            )
                        continue
                if mod_mapping.removable_ref_id and menu_item is not None:
                    target = _removable(menu_item, ref_id=mod_mapping.removable_ref_id)
                    if target is not None:
                        resolved.removed.append(target)
                        continue

            key = normalise_key(modifier.title)
            if key in add_on_by_name:
                add_on = add_on_by_name[key]
                resolved.add_ons.append((add_on, modifier))
                if add_on.is_active is False:
                    resolved.unavailable.append(
                        {"external_id": modifier.external_id, "external_text": modifier.title}
                    )
                continue
            if menu_item is not None:
                option = _option_by_name(menu_item, modifier.title)
                if option is not None:
                    resolved.options.append(option)
                    if not option["_enabled"]:
                        resolved.unavailable.append(
                            {"external_id": modifier.external_id, "external_text": modifier.title}
                        )
                    continue
            if resolved.is_ignored:
                continue
            resolved.unresolved.append(_unresolved_modifier(line, modifier))

        resolved_lines.append(resolved)

    return _ResolutionResult(lines=resolved_lines)


# ── Order creation (reuses the SAME sale/stock/VAT pipeline as the CSV path) ─


async def _existing_sale(
    db: AsyncSession, *, business: Business, provider: str, canonical: CanonicalDeliveryOrder,
) -> Order | None:
    """
    An Order already recorded for this provider order — by its id, or (from a
    provider report import, which may only carry the short display code) by
    that code within `DISPLAY_ID_MATCH_WINDOW` of the order time.
    """
    source = PLATFORM_TO_ORDER_SOURCE.get(provider, "OTHER_EXTERNAL")
    conditions = [Order.external_order_id == canonical.external_order_id]
    if canonical.display_id:
        anchor = canonical.placed_at or datetime.now(timezone.utc)
        conditions.append(and_(
            Order.external_order_id == canonical.display_id,
            Order.external_placed_at >= anchor - DISPLAY_ID_MATCH_WINDOW,
            Order.external_placed_at <= anchor + DISPLAY_ID_MATCH_WINDOW,
        ))
    rows = (
        await db.execute(
            select(Order).where(
                Order.business_id == business.id,
                Order.source == source,
                or_(*conditions),
            ).limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _choose_order_number(
    db: AsyncSession, *, business_id: uuid.UUID, provider: str, canonical: CanonicalDeliveryOrder,
) -> str:
    """
    `UE-<display id>` — the short code a courier quotes at the counter, so
    the kitchen can match bag to driver. Display codes are reused over time,
    so a code already used by this business falls back to a suffixed form,
    and an order with no display code keeps the old id-based number.
    """
    prefix = order_id_prefix(provider)
    fallback = f"{prefix}-{canonical.external_order_id}"[:32]
    if not canonical.display_id:
        return fallback
    preferred = f"{prefix}-{canonical.display_id}"[:32]
    suffixed = f"{prefix}-{canonical.display_id}-{canonical.external_order_id[:4]}"[:32]
    taken = {
        o.order_number for o in (
            await db.execute(
                select(Order).where(
                    Order.business_id == business_id,
                    Order.order_number.in_([preferred, suffixed]),
                )
            )
        ).scalars().all()
    }
    if preferred not in taken:
        return preferred
    if suffixed not in taken:
        return suffixed
    return fallback


def _order_mode(order_type: str | None) -> str:
    kind = (order_type or "").upper()
    if kind in ("PICK_UP", "PICKUP"):
        return "PICKUP"
    if kind == "DINE_IN":
        return "DINE_IN"
    return "DELIVERY"


def _kitchen_note(provider: str, canonical: CanonicalDeliveryOrder) -> str | None:
    label = _PROVIDER_LABELS.get(provider, provider)
    parts: list[str] = []
    if canonical.display_id:
        parts.append(f"{label} order {canonical.display_id}")
    if canonical.special_instructions:
        parts.append(f"Customer note: {canonical.special_instructions}")
    if canonical.cash_amount_due_cents:
        parts.append(
            f"Cash due on this order: {canonical.cash_amount_due_cents / 100:.2f} "
            f"{canonical.currency} (collected per {label}'s terms)"
        )
    if canonical.customer_phone_code:
        parts.append(f"{label} customer phone code: {canonical.customer_phone_code}")
    if canonical.fulfillment_issues:
        parts.append("Provider fulfilment issues: " + "; ".join(canonical.fulfillment_issues))
    return " | ".join(parts) or None


def _line_note(line: _ResolvedDeliveryLine) -> str | None:
    parts: list[str] = []
    if line.canonical.special_instructions:
        parts.append(line.canonical.special_instructions)
    removed_titles = [m.title for m in line.canonical.modifiers if m.removed]
    if removed_titles:
        parts.append("No " + ", ".join(removed_titles))
    if line.menu_item_name and normalise_key(line.menu_item_name) != normalise_key(line.canonical.title):
        parts.append(f"Ordered as: {line.canonical.title}")
    return " | ".join(parts) or None


async def _create_order_from_canonical(
    db: AsyncSession, *, business: Business, provider: str,
    canonical: CanonicalDeliveryOrder, resolution: _ResolutionResult,
) -> Order:
    now = datetime.now(timezone.utc)
    food_cents = _food_sale_cents(canonical) or 0
    fee_cents = max(canonical.fee_cents or 0, 0)
    # Uber's `total` is by definition what is paid out to the merchant
    # (excluding marketplace fees) — taken as reported. Only when it is
    # absent is a total built from its documented parts. How tax and
    # merchant-funded promotions show up for a South African store is not
    # confirmed by Uber's (US) examples, so no discount/tax line is derived
    # from the difference; the raw charges stay in the delivery order's
    # snapshot for reconciliation.
    total_cents = (
        canonical.total_cents
        if canonical.total_cents is not None and canonical.total_cents > 0
        else food_cents + fee_cents
    )
    vat_cents = (
        calculate_vat_cents(total_cents, business.vat_rate_basis_points, True)
        if getattr(business, "vat_registered", False) else 0
    )
    source = PLATFORM_TO_ORDER_SOURCE.get(provider, "OTHER_EXTERNAL")

    order = Order(
        business_id=business.id,
        order_number=await _choose_order_number(
            db, business_id=business.id, provider=provider, canonical=canonical,
        ),
        status=OrderStatus.ACCEPTED.value,
        payment_status="PAID",
        payment_required=False,
        payment_method=EXTERNAL_PAYMENT_METHOD,
        paid_at=canonical.placed_at or now,
        till_session_id=None,
        cash_tendered_cents=None,
        change_due_cents=None,
        source=source,
        order_mode=_order_mode(canonical.order_type),
        subtotal_cents=food_cents,
        delivery_fee_cents=fee_cents,
        discount_cents=0,
        tax_cents=vat_cents,
        total_cents=total_cents,
        currency=canonical.currency or getattr(business, "currency", "ZAR") or "ZAR",
        customer_name=(canonical.customer_first_name or None),
        phone_number=(canonical.customer_phone or None) and canonical.customer_phone[:32],
        delivery_address=None,  # never available from the provider — see canonical.py
        special_instructions=_kitchen_note(provider, canonical),
        external_order_id=canonical.external_order_id,
        external_placed_at=canonical.placed_at,
        external_imported_at=now,
        external_status=(canonical.provider_status_raw or None) and canonical.provider_status_raw[:48],
        estimated_ready_at=canonical.ready_for_pickup_at,
        confirmed_at=canonical.placed_at or now,
        accepted_at=now,
    )
    db.add(order)
    await db.flush()

    resolved_items = []
    for line in resolution.lines:
        if line.is_ignored or not line.menu_item_id:
            continue

        quantity = line.canonical.quantity
        unit = line.canonical.unit_price_cents
        line_total = line.canonical.total_price_cents
        if line_total is None and unit is not None:
            line_total = unit * quantity
        options_snapshot = [{k: v for k, v in o.items() if not k.startswith("_")} for o in line.options]
        add_ons_snapshot = [
            {
                "add_on_id": str(add_on.id),
                "name": add_on.name,
                "quantity": modifier.quantity,
                "price_cents": modifier.price_cents or 0,
            }
            for add_on, modifier in line.add_ons
        ]
        add_on_total = sum(a["price_cents"] * a["quantity"] for a in add_ons_snapshot)
        base = line.canonical.base_unit_price_cents
        if base is None:
            base = (unit - add_on_total) if unit is not None else 0
        option_adjustment = max((unit or 0) - base - add_on_total, 0) if unit is not None else 0

        db.add(OrderItem(
            order_id=order.id,
            business_id=business.id,
            menu_item_id=line.menu_item_id,
            name_snapshot=line.menu_item_name or line.canonical.title,
            base_price_cents=max(base, 0),
            option_adjustment_cents=option_adjustment,
            add_on_total_cents=add_on_total,
            unit_price_cents=unit or 0,
            quantity=quantity,
            line_total_cents=line_total or 0,
            selected_options_snapshot=options_snapshot or None,
            add_ons_snapshot=add_ons_snapshot or None,
            removed_ingredients_snapshot=line.removed or None,
            special_instructions=_line_note(line),
        ))

        import types as _types
        resolved_items.append(_types.SimpleNamespace(
            menu_item_id=line.menu_item_id,
            breakdown=_types.SimpleNamespace(quantity=quantity),
            selected_options_snapshot=options_snapshot,
            add_ons_snapshot=add_ons_snapshot,
            removed_ingredients_snapshot=line.removed,
        ))

    db.add(OrderEvent(
        order_id=order.id, business_id=business.id, old_status=None,
        new_status=OrderStatus.ACCEPTED.value,
        reason=f"Accepted via {provider} order {canonical.display_id or canonical.external_order_id}",
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

#: BUG-0093: bound how many rows one tick claims, so a large backlog drains
#: over several 30s ticks instead of one tick holding row locks (and a
#: worker DB connection) for the entire backlog at once.
_WATCHDOG_BATCH_LIMIT = 100

_WATCHDOG_DENY_REASON_CODE = "OTHER"
_WATCHDOG_DENY_EXPLANATION = (
    "Accept/deny deadline missed — auto-denied by the accept-deadline watchdog."
)


async def delivery_accept_deadline_watchdog(ctx: dict) -> None:
    """
    Auto-deny anything past `accept_deadline_at` that's still open
    (RECEIVED / FETCHING / NEEDS_MAPPING / READY_TO_ACCEPT). NEVER
    auto-accepts, NEVER silently drops — always an active `deny_pos_order`
    call with a clear reason code, honest to both the customer (they get a
    real denial, not indefinite silence) and the books (no phantom sale).
    ACCEPT_UNCONFIRMED is deliberately NOT scanned: a real sale stands
    behind it and the kitchen is making the food.

    BUG-0093 fix — TWO PHASES, claim-then-notify:
    Phase 1 (below, inside the row lock/transaction): push the deadline
    comparison into SQL, cap the batch with a LIMIT so a large backlog
    drains over several ticks, and CLAIM each due row (flip it to DENIED, no
    network call) and commit BEFORE any outbound provider call is made. A
    slow/hung/timed-out `deny_pos_order` call for one tenant can therefore
    never hold another tenant's row lock or this worker's DB connection
    hostage.
    Phase 2 (after the `async with` block closes, so no DB transaction is
    open): the actual `deny_pos_order` network calls, best-effort — a
    failure here is logged and otherwise ignored, exactly the same "marked
    DENIED locally regardless; Uber's own 11.5-minute timeout is the
    backstop" honesty `_issue_deny` already uses for every other deny path.
    The Python-side deadline re-check is kept alongside the SQL predicate as
    a belt-and-suspenders guard (also lets a test double that doesn't
    evaluate SQL WHERE clauses exercise this function correctly).
    """
    settings = get_settings()
    grace = settings.DELIVERY_DEADLINE_GRACE_SECONDS
    cutoff = datetime.now(timezone.utc)
    deadline_threshold = cutoff - timedelta(seconds=grace)

    claimed: list[dict] = []

    async with ctx["session_factory"]() as db:
        try:
            rows = (
                await db.execute(
                    select(DeliveryOrder)
                    .where(
                        DeliveryOrder.status.in_(list(OPEN_STATUSES)),
                        DeliveryOrder.accept_deadline_at.isnot(None),
                        DeliveryOrder.accept_deadline_at <= deadline_threshold,
                    )
                    .order_by(DeliveryOrder.accept_deadline_at.asc())
                    .limit(_WATCHDOG_BATCH_LIMIT)
                    .with_for_update(skip_locked=True)
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
                # Claim locally FIRST — no network call while the row lock /
                # transaction is open. Mirrors _issue_deny's own local
                # write, just without the (deferred) adapter call.
                _transition(db, row, "DENIED", reason="accept deadline missed (watchdog)")
                row.denied_at = datetime.now(timezone.utc)
                row.deny_reason_code = _WATCHDOG_DENY_REASON_CODE
                row.deny_reason_text = _WATCHDOG_DENY_EXPLANATION
                claimed.append({
                    "provider": row.provider,
                    "business_id": row.business_id,
                    "external_order_id": row.external_order_id,
                    "external_store_id": row.external_store_id,
                    "connection_id": connection.id if connection else None,
                    "environment": connection.environment if connection else "production",
                })
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("DELIVERY_ACCEPT_DEADLINE_WATCHDOG_FAILED")
            return

    # Phase 2 — network calls OUTSIDE the lock/transaction (BUG-0093). Each
    # row is already terminal (DENIED) in the database regardless of whether
    # this call succeeds.
    for item in claimed:
        adapter = registry.get_provider(item["provider"])
        context = DeliveryOrderContext(
            business_id=item["business_id"],
            connection_id=item["connection_id"] or uuid.uuid4(),
            external_store_id=item["external_store_id"],
            environment=item["environment"],
        )
        try:
            if adapter is not None:
                await adapter.deny_order(
                    external_order_id=item["external_order_id"], context=context,
                    reason_code=_WATCHDOG_DENY_REASON_CODE,
                    explanation=_WATCHDOG_DENY_EXPLANATION,
                )
        except (DeliveryProviderNotImplemented, DeliveryProviderError, DeliveryProviderTimeout):
            logger.exception(
                "DELIVERY_DENY_CALL_FAILED (watchdog): business=%s external_order_id=%s — "
                "already marked DENIED locally; Uber's own 11.5-minute timeout is the backstop.",
                item["business_id"], item["external_order_id"],
            )
        except Exception:
            logger.exception(
                "DELIVERY_DENY_CALL_UNEXPECTED_ERROR (watchdog): business=%s external_order_id=%s",
                item["business_id"], item["external_order_id"],
            )
