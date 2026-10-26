"""
Uber Eats marketplace flow against a REAL Postgres — the properties a mocked
session cannot honestly prove: the stacked unique constraints under genuinely
concurrent duplicate webhook deliveries, exactly-once stock effects, tenant
resolution from the store id alone, the cancellation stock rule, and the
CSV/live-API double-booking guard.

Everything Uber-side is real code (HMAC signature verification, envelope and
Get-Order-Details parsing against Uber's documented payload shape) except the
four outbound network calls (`_fetch_order` / `_accept_order` / `_deny_order` /
`_mark_order_ready`), which are replaced at the adapter's network seam. Run
with the dedicated test environment (TESTING.md, "Integration-testing
approach") — never against a shared database.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import routes_delivery
from backend.app.core.config import get_settings
from backend.app.core.rbac import AuthUser
from backend.app.delivery import order_service
from backend.app.delivery.errors import DeliveryProviderError
from backend.app.delivery.intake import ingest_delivery_webhook
from backend.app.delivery.uber import mapping as uber_mapping
from backend.app.delivery.uber.adapter import UberEatsAdapter
from backend.app.external_orders.adapters import CanonicalExternalOrder, ExternalOrderLine
from backend.app.external_orders.service import (
    import_resolved_orders,
    normalise_key,
    resolve_orders,
)
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent
from shared.models.external_orders import ExternalProductMapping
from shared.models.inventory import Ingredient, StockMovement
from shared.models.menu import MenuItem
from shared.models.order import Order
from shared.models.recipe import Recipe, RecipeItem
from shared.utils import generate_business_code
from tests.integration._db import get_session_factory

SECRET = "integration-test-uber-client-secret"
SUGAR_REF = "rm-sugar"


# ── Fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def uber_settings():
    settings = get_settings()
    with patch.multiple(
        settings,
        UBER_EATS_WEBHOOK_ENABLED=True,
        UBER_EATS_CLIENT_ID="it-client-id",
        UBER_EATS_CLIENT_SECRET=SECRET,
        UBER_EATS_SANDBOX_CLIENT_ID="",
        UBER_EATS_SANDBOX_CLIENT_SECRET="",
    ):
        yield settings


@pytest.fixture(autouse=True)
def no_notifications():
    """Redis pub/sub + web push are best-effort side channels, not under test here."""
    with patch.object(order_service, "_publish_alert", AsyncMock()), \
         patch.object(order_service, "_publish_order_cancelled", AsyncMock()):
        yield


class RecordingPool:
    """Stands in for the ARQ pool: records jobs so the test runs them for real."""

    def __init__(self):
        self.jobs: list[tuple[str, tuple]] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, args))


_JOBS = {
    "process_delivery_order": order_service.process_delivery_order,
    "process_delivery_cancellation": order_service.process_delivery_cancellation,
    "sync_delivery_order_status": order_service.sync_delivery_order_status,
}


async def _run_jobs(pool: RecordingPool, *, concurrently: bool = False) -> None:
    ctx = {"session_factory": get_session_factory()}
    jobs, pool.jobs = pool.jobs, []
    calls = [_JOBS[name](ctx, *args) for name, args in jobs]
    if concurrently:
        await asyncio.gather(*calls)
    else:
        for call in calls:
            await call


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {
        "x-uber-signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-environment": "production",
    }


def _webhook(store_id: str, order_id: str, *, event_type="orders.notification", event_id=None):
    return _signed({
        "event_type": event_type,
        "event_id": event_id or str(uuid.uuid4()),
        "event_time": 1729651095,
        "meta": {"resource_id": order_id, "status": "pos", "user_id": store_id},
        "resource_href": f"https://api.uber.com/v2/eats/order/{order_id}",
    })


async def _deliver(body: bytes, headers: dict, pool: RecordingPool):
    async with get_session_factory()() as session:
        return await ingest_delivery_webhook(
            session, provider="UBER_EATS", raw_body=body, headers=headers, arq_pool=pool,
        )


def _uber_order(order_id: str, store_id: str, *, display_id="BC953", quantity=2, remove_sugar=False):
    """A Get Order Details response in Uber's documented v2 shape, through the real parser."""
    def money(amount):
        return {"amount": amount, "currency_code": "ZAR", "formatted_amount": f"R{amount / 100:.2f}"}

    groups = []
    if remove_sugar:
        groups.append({
            "id": "Add-sugar", "title": "Add sugar", "selected_items": None,
            "removed_items": [{"id": "Sugar", "title": "Sugar", "quantity": 0, "price": {"unit_price": money(0)}}],
        })
    raw = {
        "id": order_id,
        "display_id": display_id,
        "current_state": "CREATED",
        "type": "DELIVERY_BY_UBER",
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "eater": {"first_name": "Larry", "phone": "+27 21 555 0100", "phone_code": "555 55 555"},
        "cart": {"items": [{
            "id": "Muffin", "title": "Fresh-baked muffin", "quantity": quantity,
            "price": {
                "unit_price": money(3500), "total_price": money(3500 * quantity),
                "base_unit_price": money(3500),
            },
            "selected_modifier_groups": groups,
        }]},
        "payment": {"charges": {
            "sub_total": money(3500 * quantity), "tax": money(0),
            "total_fee": money(0), "total": money(3500 * quantity),
        }},
    }
    return uber_mapping.parse_order_details(raw, external_store_id=store_id)


async def _make_business(db: AsyncSession, *, name: str) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"{name} {unique}", slug=f"it-delivery-{unique}", business_code=generate_business_code(),
        currency="ZAR", timezone="Africa/Johannesburg",
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _drop_business(db: AsyncSession, business_id: uuid.UUID) -> None:
    # Webhook events are ON DELETE SET NULL (an unknown store's event has no
    # business at all), so they are removed explicitly, like AuditEvent.
    await db.execute(delete(DeliveryWebhookEvent).where(DeliveryWebhookEvent.business_id == business_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
    await db.execute(delete(Business).where(Business.id == business_id))
    await db.commit()


async def _setup_uber_tenant(db: AsyncSession, business: Business, *, auto_accept: bool = True) -> dict:
    business.delivery_platforms_enabled = True
    business.uber_eats_enabled = True
    business.inventory_enabled = True
    business.is_active = True
    db.add(business)

    flour = Ingredient(
        business_id=business.id, name=f"Flour {uuid.uuid4().hex[:6]}", canonical_unit="GRAM",
        cost_per_canonical_micro_cents=0, purchase_pack_milli=1000,
    )
    sugar = Ingredient(
        business_id=business.id, name=f"Sugar {uuid.uuid4().hex[:6]}", canonical_unit="GRAM",
        cost_per_canonical_micro_cents=0, purchase_pack_milli=1000,
    )
    menu_item = MenuItem(
        business_id=business.id, name="Fresh-baked muffin", price_cents=3000,
        options_json={"removable_ingredients": [{"id": SUGAR_REF, "name": "Sugar"}]},
    )
    db.add_all([flour, sugar, menu_item])
    await db.flush()

    recipe = Recipe(business_id=business.id, menu_item_id=menu_item.id, yield_qty=1)
    db.add(recipe)
    await db.flush()
    db.add_all([
        RecipeItem(business_id=business.id, recipe_id=recipe.id, ingredient_id=flour.id, quantity_milli=100_000),
        RecipeItem(
            business_id=business.id, recipe_id=recipe.id, ingredient_id=sugar.id,
            quantity_milli=10_000, removable_ref_id=SUGAR_REF,
        ),
    ])
    db.add(ExternalProductMapping(
        business_id=business.id, platform="UBER_EATS", kind="ITEM",
        external_text="Fresh-baked muffin", external_key=normalise_key("Fresh-baked muffin"),
        external_id="Muffin", menu_item_id=menu_item.id, status="MAPPED",
    ))
    store_id = f"it-store-{uuid.uuid4().hex[:12]}"
    db.add(DeliveryConnection(
        business_id=business.id, provider="UBER_EATS", external_store_id=store_id,
        status="CONNECTED", environment="production", auto_accept_enabled=auto_accept,
    ))
    await db.commit()
    return {"store_id": store_id, "flour": flour, "sugar": sugar, "menu_item": menu_item}


@pytest.fixture
async def uber_tenant(test_business: Business, db: AsyncSession):
    tenant = await _setup_uber_tenant(db, test_business)
    tenant["business"] = test_business
    yield tenant
    await db.execute(delete(DeliveryWebhookEvent).where(DeliveryWebhookEvent.business_id == test_business.id))
    await db.execute(delete(DeliveryWebhookEvent).where(DeliveryWebhookEvent.external_store_id == "store-nobody-owns"))
    await db.commit()


async def _count(model, *conditions) -> int:
    async with get_session_factory()() as session:
        return (await session.execute(select(func.count()).select_from(model).where(*conditions))).scalar_one()


async def _stock_movements(order_id: uuid.UUID) -> list[StockMovement]:
    async with get_session_factory()() as session:
        return list((await session.execute(
            select(StockMovement).where(StockMovement.source_id == order_id)
        )).scalars().all())


async def _delivery_row(business_id, order_id: str) -> DeliveryOrder | None:
    async with get_session_factory()() as session:
        return (await session.execute(select(DeliveryOrder).where(
            DeliveryOrder.business_id == business_id, DeliveryOrder.external_order_id == order_id,
        ))).scalar_one_or_none()


async def _live_accepted_order(tenant: dict, *, order_id=None, display_id="BC953", remove_sugar=False):
    """Webhook -> intake -> worker -> accepted Order, all real except Uber's network."""
    order_id = order_id or str(uuid.uuid4())
    pool = RecordingPool()
    canonical = _uber_order(order_id, tenant["store_id"], display_id=display_id, remove_sugar=remove_sugar)
    with patch.object(UberEatsAdapter, "_fetch_order", AsyncMock(return_value=canonical)), \
         patch.object(UberEatsAdapter, "_accept_order", AsyncMock()) as accept:
        result = await _deliver(*_webhook(tenant["store_id"], order_id), pool)
        assert result.accepted and result.error_category == "OK"
        await _run_jobs(pool)
    row = await _delivery_row(tenant["business"].id, order_id)
    return order_id, row, accept


# ── Exactly-once under duplicate delivery ───────────────────────────────────


async def test_concurrent_duplicate_webhooks_book_one_sale_and_one_stock_deduction(uber_tenant):
    business = uber_tenant["business"]
    order_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    pool = RecordingPool()
    canonical = _uber_order(order_id, uber_tenant["store_id"])

    with patch.object(UberEatsAdapter, "_fetch_order", AsyncMock(return_value=canonical)), \
         patch.object(UberEatsAdapter, "_accept_order", AsyncMock()) as accept:
        # Uber retries an unacknowledged event up to 7 times; simulate five
        # copies of the SAME event arriving at once, plus a second, distinct
        # event for the same order.
        same_event = [_deliver(*_webhook(uber_tenant["store_id"], order_id, event_id=event_id), pool) for _ in range(5)]
        results = await asyncio.gather(*same_event)
        await _deliver(*_webhook(uber_tenant["store_id"], order_id), pool)
        assert all(r.http_status == 200 for r in results)
        # Run every enqueued job at the same time — the row lock decides.
        await _run_jobs(pool, concurrently=True)

    assert await _count(DeliveryWebhookEvent, DeliveryWebhookEvent.business_id == business.id) == 2
    assert await _count(DeliveryOrder, DeliveryOrder.business_id == business.id) == 1
    orders = await _count(Order, Order.business_id == business.id, Order.external_order_id == order_id)
    assert orders == 1, "one real sale, however many times Uber delivered the event"
    assert accept.await_count == 1, "Uber is told 'accepted' exactly once"

    row = await _delivery_row(business.id, order_id)
    assert row.status == "ACCEPTED"
    movements = await _stock_movements(row.order_id)
    assert sorted(m.quantity_milli for m in movements) == [-200_000, -20_000], (
        "2 muffins x (100g flour + 10g sugar), deducted exactly once"
    )


async def test_order_lines_carry_the_channel_price_and_the_kitchen_code(uber_tenant):
    order_id, row, _ = await _live_accepted_order(uber_tenant, display_id="QX123")
    async with get_session_factory()() as session:
        order = await session.get(Order, row.order_id)
        await session.refresh(order, attribute_names=["items"])
        assert order.order_number == "UE-QX123", "the code a courier quotes at the counter"
        assert order.subtotal_cents == 7000 and order.total_cents == 7000
        assert order.till_session_id is None and order.payment_method == "EXTERNAL_PLATFORM"
        assert len(order.items) == 1
        item = order.items[0]
        assert (item.quantity, item.unit_price_cents, item.line_total_cents) == (2, 3500, 7000)


async def test_removed_default_ingredient_is_not_deducted(uber_tenant):
    order_id, row, _ = await _live_accepted_order(uber_tenant, remove_sugar=True)
    movements = await _stock_movements(row.order_id)
    assert [m.ingredient_id for m in movements] == [uber_tenant["flour"].id], "no sugar -> no sugar used"


# ── Tenant isolation ─────────────────────────────────────────────────────────


async def test_store_id_alone_selects_the_tenant_and_staff_only_see_their_own_queue(uber_tenant, db):
    other = await _make_business(db, name="Other Restaurant")
    try:
        other_tenant = await _setup_uber_tenant(db, other, auto_accept=False)
        pool = RecordingPool()
        order_id = str(uuid.uuid4())
        canonical = _uber_order(order_id, other_tenant["store_id"])
        with patch.object(UberEatsAdapter, "_fetch_order", AsyncMock(return_value=canonical)):
            await _deliver(*_webhook(other_tenant["store_id"], order_id), pool)
            await _run_jobs(pool)

        assert await _delivery_row(uber_tenant["business"].id, order_id) is None
        other_row = await _delivery_row(other.id, order_id)
        assert other_row.status == "READY_TO_ACCEPT"

        def staff(business_id):
            return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")

        async with get_session_factory()() as session:
            mine = await routes_delivery.staff_queue(user=staff(uber_tenant["business"].id), db=session)
            theirs = await routes_delivery.staff_queue(user=staff(other.id), db=session)
        assert [q.id for q in mine] == []
        assert [q.id for q in theirs] == [other_row.id]
        assert theirs[0].can_accept is True and theirs[0].lines[0]["title"] == "Fresh-baked muffin"

        # A store nobody owns: logged, acknowledged, never an order.
        unknown = await _deliver(*_webhook("store-nobody-owns", str(uuid.uuid4())), pool)
        assert unknown.error_category == "UNKNOWN_STORE" and pool.jobs == []
    finally:
        await _drop_business(db, other.id)


# ── Cancellation follows the platform stock rule ────────────────────────────


async def test_cancel_before_preparation_restores_stock_exactly_once(uber_tenant):
    order_id, row, _ = await _live_accepted_order(uber_tenant)
    pool = RecordingPool()
    for _ in range(2):  # a retried cancel webhook must not restore stock twice
        await _deliver(*_webhook(uber_tenant["store_id"], order_id, event_type="orders.cancel"), pool)
    await _run_jobs(pool)

    async with get_session_factory()() as session:
        order = await session.get(Order, row.order_id)
        assert order.status == "CANCELLED"
    movements = await _stock_movements(row.order_id)
    assert sum(m.quantity_milli for m in movements) == 0, "the reversal nets the sale to zero"
    assert sum(1 for m in movements if m.movement_type == "SALE_REVERSAL") == 2
    assert (await _delivery_row(uber_tenant["business"].id, order_id)).status == "CANCELLED"


async def test_cancel_after_preparation_started_keeps_the_deduction(uber_tenant):
    from backend.app.api.v1.routes_orders import StatusUpdateRequest, update_order_status

    order_id, row, _ = await _live_accepted_order(uber_tenant)
    staff = AuthUser(user_id=uuid.uuid4(), business_id=uuid_tenant(uber_tenant), role="STAFF", token_type="access")
    async with get_session_factory()() as session:
        await update_order_status(row.order_id, StatusUpdateRequest(status="IN_PROGRESS"), user=staff, db=session)

    pool = RecordingPool()
    await _deliver(*_webhook(uber_tenant["store_id"], order_id, event_type="orders.cancel"), pool)
    await _run_jobs(pool)

    movements = await _stock_movements(row.order_id)
    assert all(m.movement_type == "SALE_CONSUMPTION" for m in movements), "the food was made"
    assert await _count(
        AuditEvent, AuditEvent.target_id == row.order_id, AuditEvent.action == "inventory.sale.stock_kept_consumed",
    ) == 1


def uuid_tenant(tenant: dict) -> uuid.UUID:
    return tenant["business"].id


# ── A refused accept books nothing ───────────────────────────────────────────


async def test_provider_refusing_the_accept_leaves_no_sale_and_no_stock(uber_tenant):
    business = uber_tenant["business"]
    order_id = str(uuid.uuid4())
    pool = RecordingPool()
    canonical = _uber_order(order_id, uber_tenant["store_id"])
    refused = AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "order expired", status_code=409))
    with patch.object(UberEatsAdapter, "_fetch_order", AsyncMock(return_value=canonical)), \
         patch.object(UberEatsAdapter, "_accept_order", refused):
        await _deliver(*_webhook(uber_tenant["store_id"], order_id), pool)
        await _run_jobs(pool)

    row = await _delivery_row(business.id, order_id)
    assert row.status == "FAILED" and row.order_id is None
    assert await _count(Order, Order.business_id == business.id, Order.external_order_id == order_id) == 0
    assert await _count(StockMovement, StockMovement.business_id == business.id) == 0


# ── Kitchen -> provider status push ─────────────────────────────────────────


async def test_ready_is_pushed_to_the_provider_and_hand_over_closes_the_row(uber_tenant):
    order_id, row, _ = await _live_accepted_order(uber_tenant)
    pool = RecordingPool()
    business_id = str(uuid_tenant(uber_tenant))
    pool.jobs.append(("sync_delivery_order_status", (business_id, str(row.order_id), "READY", None)))
    with patch.object(UberEatsAdapter, "_mark_order_ready", AsyncMock()) as ready:
        await _run_jobs(pool)
    ready.assert_awaited_once()
    after_ready = await _delivery_row(uber_tenant["business"].id, order_id)
    assert after_ready.last_provider_sync_at is not None and after_ready.last_provider_sync_error is None

    pool.jobs.append(("sync_delivery_order_status", (business_id, str(row.order_id), "COLLECTED", None)))
    await _run_jobs(pool)
    assert (await _delivery_row(uber_tenant["business"].id, order_id)).status == "FULFILLED"

    # A job carrying ANOTHER tenant's id can never touch this row.
    pool.jobs.append(("sync_delivery_order_status", (str(uuid.uuid4()), str(row.order_id), "CANCELLED", None)))
    with patch.object(UberEatsAdapter, "_cancel_order", AsyncMock()) as cancel:
        await _run_jobs(pool)
    cancel.assert_not_awaited()


# ── Watchdog ─────────────────────────────────────────────────────────────────


async def test_watchdog_denies_overdue_open_orders_but_never_an_unconfirmed_accept(uber_tenant, db):
    business = uber_tenant["business"]
    connection = (await db.execute(select(DeliveryConnection).where(
        DeliveryConnection.business_id == business.id))).scalar_one()
    overdue = datetime.now(timezone.utc) - timedelta(minutes=30)
    open_row = DeliveryOrder(
        business_id=business.id, provider="UBER_EATS", delivery_connection_id=connection.id,
        external_order_id=str(uuid.uuid4()), external_store_id=uber_tenant["store_id"],
        status="NEEDS_MAPPING", accept_deadline_at=overdue,
    )
    unconfirmed = DeliveryOrder(
        business_id=business.id, provider="UBER_EATS", delivery_connection_id=connection.id,
        external_order_id=str(uuid.uuid4()), external_store_id=uber_tenant["store_id"],
        status="ACCEPT_UNCONFIRMED", accept_deadline_at=overdue,
    )
    db.add_all([open_row, unconfirmed])
    await db.commit()

    with patch.object(UberEatsAdapter, "_deny_order", AsyncMock()) as deny:
        await order_service.delivery_accept_deadline_watchdog({"session_factory": get_session_factory()})

    assert (await _delivery_row(business.id, open_row.external_order_id)).status == "DENIED"
    assert (await _delivery_row(business.id, unconfirmed.external_order_id)).status == "ACCEPT_UNCONFIRMED"
    assert deny.await_count == 1


# ── CSV report vs live API: one real sale, one record ───────────────────────


async def test_report_import_skips_an_order_the_live_api_already_booked(uber_tenant, db):
    business = uber_tenant["business"]
    _, row, _ = await _live_accepted_order(uber_tenant, display_id="RP777")
    now = datetime.now(timezone.utc)

    def report_order(order_code):
        return CanonicalExternalOrder(
            external_order_id=order_code, placed_at=now,
            lines=[ExternalOrderLine(external_item_text="Fresh-baked muffin", quantity=1, gross_cents=3500)],
            gross_cents=3500,
        )

    summary = await resolve_orders(
        db, business_id=business.id, platform="UBER_EATS",
        orders=[report_order("RP777"), report_order("ZZ999")],
    )
    by_id = {o.external_order_id: o for o in summary.orders}
    assert by_id["RP777"].is_duplicate and by_id["RP777"].duplicate_source == "LIVE_API"
    assert not by_id["ZZ999"].is_duplicate

    outcome = await import_resolved_orders(
        db, business=business, platform="UBER_EATS", batch_id=uuid.uuid4(),
        summary=summary, user_id=None,
    )
    await db.commit()
    assert outcome.orders_created == 1 and outcome.duplicates_skipped == 1
    assert await _count(Order, Order.business_id == business.id, Order.external_order_id == "RP777") == 0, (
        "the live order is booked under Uber's id; the report's short code must not add a second sale"
    )


# ── Environment isolation (security review, 2026-10-26) ─────────────────────


async def test_a_cancel_signed_with_the_sandbox_secret_cannot_touch_a_production_order(uber_tenant, uber_settings):
    """
    The cancellation job has no environment gate of its own, so before the
    intake check a cancel signed with the (weaker-held) sandbox secret,
    naming a production store and order, cancelled that order and reversed
    its stock. Intake now refuses it before any row or job exists.
    """
    order_id, row, _ = await _live_accepted_order(uber_tenant)
    sandbox_secret = "integration-test-sandbox-secret"
    body = json.dumps({
        "event_type": "orders.cancel", "event_id": str(uuid.uuid4()),
        "meta": {"resource_id": order_id, "status": "pos", "user_id": uber_tenant["store_id"]},
    }).encode()
    headers = {"x-uber-signature": hmac.new(sandbox_secret.encode(), body, hashlib.sha256).hexdigest()}
    pool = RecordingPool()
    with patch.object(uber_settings, "UBER_EATS_SANDBOX_CLIENT_SECRET", sandbox_secret):
        result = await _deliver(body, headers, pool)

    assert result.error_category == "ENV_MISMATCH" and pool.jobs == []
    async with get_session_factory()() as session:
        assert (await session.get(Order, row.order_id)).status == "ACCEPTED"
    assert (await _delivery_row(uber_tenant["business"].id, order_id)).status == "ACCEPTED"
    assert all(m.movement_type == "SALE_CONSUMPTION" for m in await _stock_movements(row.order_id))
