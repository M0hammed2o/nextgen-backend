"""
Real-DB tests proving BUG-0043's fix: every analytics-reporting endpoint
(summary, daily, top-items, external-orders channel report) agrees on the
same sale/gross/refund/net/void definition (backend/app/analytics/revenue.py)
when aggregated by real Postgres. A mocked session cannot prove SQL
aggregation, timezone bucketing, or cross-tenant isolation, so these run
against the throwaway local Postgres instance (see tests/integration/conftest.py).

Requires DATABASE_URL to point at a reachable real Postgres, e.g.:

  DATABASE_URL=postgresql+asyncpg://nextgen:***@localhost:55444/nextgen_test_analytics \
  python -m pytest tests/integration/test_analytics_revenue.py -q
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete

from backend.app.api.v1.routes_analytics import get_daily_breakdown, get_summary, get_top_items
from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_external_orders import channel_breakdown
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSRefundRequest,
    POSVoidRequest,
    TillOpenRequest,
    create_pos_order,
    open_till,
    refund_pos_order,
    void_pos_order,
)
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order
from shared.utils import generate_business_code
from shared.utils.time import local_midnight_to_utc, today_date_for_business


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _make_business(db, **overrides) -> Business:
    """Same shape as tests/integration/conftest.py's `test_business` fixture,
    for tests that need a SECOND, independent tenant (tenant-isolation
    checks) — pytest fixtures aren't easily requested twice per test."""
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name=f"Analytics Test Restaurant {unique}",
        slug=f"analytics-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
    )
    defaults.update(overrides)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _cleanup_business(db, business: Business) -> None:
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business.id))
    await db.execute(delete(Business).where(Business.id == business.id))
    await db.commit()


async def _pos_setup(db, business: Business):
    """Open a till and create two menu items: one grab-and-go (no kitchen
    prep -> completes immediately) and one needing prep (-> ACCEPTED, still
    counts as a sale -- this is the exact case BUG-0043 missed)."""
    owner = _owner(business.id)
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    category = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=db)
    grab_and_go = await create_item(
        body=ItemCreate(category_id=category.id, name="Coke", price_cents=20000, requires_preparation=False),
        user=owner, db=db,
    )
    needs_kitchen = await create_item(
        body=ItemCreate(category_id=category.id, name="Burger", price_cents=30000, requires_preparation=True),
        user=owner, db=db,
    )
    grab_and_go_cheap = await create_item(
        body=ItemCreate(category_id=category.id, name="Chips", price_cents=10000, requires_preparation=False),
        user=owner, db=db,
    )
    return owner, grab_and_go, needs_kitchen, grab_and_go_cheap


async def test_summary_daily_topitems_and_channels_agree_on_owner_scenario(db, test_business: Business):
    """
    The owner's exact scenario:
      POS cash R200 (COMPLETED/PAID) + POS card R300 (ACCEPTED/PAID, still in
      kitchen) + WhatsApp R150 (COLLECTED, paid) + a R50 partial refund on the
      POS card sale + a voided R100 POS sale + a CANCELLED R80 WhatsApp order
      + an UNPAID NEW R60 WhatsApp order
      => gross 65000c, refunds 5000c, net 60000c, voided excluded,
         cancelled/unpaid-new excluded -- identically from every endpoint.
    """
    owner, coke, burger, chips = await _pos_setup(db, test_business)

    # POS cash sale, grab-and-go -> COMPLETED immediately.
    pos_cash = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=coke.id, quantity=1)],
            payment_method="CASH", cash_tendered_cents=20000,
        ),
        user=owner, db=db,
    )
    assert pos_cash.status == "COMPLETED" and pos_cash.payment_status == "PAID"

    # POS card sale, needs kitchen prep -> ACCEPTED, already PAID.
    pos_card = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=burger.id, quantity=1)],
            payment_method="CARD",
        ),
        user=owner, db=db,
    )
    assert pos_card.status == "ACCEPTED" and pos_card.payment_status == "PAID"

    # Partial refund of R50 on the POS card sale.
    refunded = await refund_pos_order(
        order_id=pos_card.id, body=POSRefundRequest(amount_cents=5000, reason="Test partial refund"),
        user=owner, db=db,
    )
    assert refunded.payment_status == "PARTIALLY_REFUNDED"
    assert refunded.refund_amount_cents == 5000

    # Voided R100 POS sale.
    pos_void = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=chips.id, quantity=1)],
            payment_method="CASH", cash_tendered_cents=10000,
        ),
        user=owner, db=db,
    )
    voided = await void_pos_order(order_id=pos_void.id, body=POSVoidRequest(reason="Mis-rung"), user=owner, db=db)
    assert voided.status == "CANCELLED" and voided.payment_status == "VOIDED"

    # WhatsApp COLLECTED, cash-on-collection -- payment_status never PAID.
    wa_collected = Order(
        business_id=test_business.id, order_number="WA-TEST-COLLECTED", status="COLLECTED",
        payment_status="CASH_ON_COLLECTION", order_mode="DELIVERY", source="WHATSAPP",
        subtotal_cents=15000, total_cents=15000, currency="ZAR", phone_number="0821110001",
    )
    # CANCELLED WhatsApp order, never paid -- a true cancellation.
    wa_cancelled = Order(
        business_id=test_business.id, order_number="WA-TEST-CANCELLED", status="CANCELLED",
        payment_status="UNPAID", order_mode="DELIVERY", source="WHATSAPP",
        subtotal_cents=8000, total_cents=8000, currency="ZAR", phone_number="0821110002",
    )
    # UNPAID NEW WhatsApp order -- still in the funnel, not a sale yet.
    wa_new = Order(
        business_id=test_business.id, order_number="WA-TEST-NEW", status="NEW",
        payment_status="UNPAID", order_mode="DELIVERY", source="WHATSAPP",
        subtotal_cents=6000, total_cents=6000, currency="ZAR", phone_number="0821110003",
    )
    db.add_all([wa_collected, wa_cancelled, wa_new])
    await db.commit()

    # ── Summary ──────────────────────────────────────────────────────────
    summary = await get_summary(period="today", user=owner, db=db)
    assert summary.total_orders == 6
    assert summary.completed_orders == 3  # pos_cash, pos_card, wa_collected
    assert summary.cancelled_orders == 1  # wa_cancelled only
    assert summary.voided_orders == 1
    assert summary.gross_cents == 65000
    assert summary.refunds_cents == 5000
    assert summary.net_cents == 60000
    assert summary.revenue_cents == 60000  # existing field, now NET
    assert summary.voided_cents == 10000

    # ── Daily breakdown -- same day, same totals ────────────────────────
    daily = await get_daily_breakdown(days=1, user=owner, db=db)
    assert len(daily) == 1
    today_row = daily[0]
    assert today_row.orders == 6
    assert today_row.gross_cents == 65000
    assert today_row.refunds_cents == 5000
    assert today_row.revenue_cents == 60000

    # ── Top items -- voided Chips excluded, partially-refunded Burger kept ──
    top_items = await get_top_items(days=1, limit=10, user=owner, db=db)
    by_name = {i.name: i for i in top_items}
    assert "Chips" not in by_name  # voided order's line excluded
    assert by_name["Coke"].quantity == 1
    assert by_name["Coke"].revenue_cents == 20000
    assert by_name["Burger"].quantity == 1
    assert by_name["Burger"].revenue_cents == 30000  # line total, refund is order-level only

    # ── External-orders channel report -- same numbers, split by source ────
    channels = await channel_breakdown(days=1, user=owner, db=db)
    by_source = {c["source"]: c for c in channels["channels"]}
    assert by_source["POS"]["order_count"] == 2
    assert by_source["POS"]["gross_cents"] == 50000
    assert by_source["POS"]["refunds_cents"] == 5000
    assert by_source["POS"]["net_cents"] == 45000
    assert by_source["WHATSAPP"]["order_count"] == 1
    assert by_source["WHATSAPP"]["gross_cents"] == 15000
    assert "Chips" not in str(by_source)  # sanity: voided order not lurking anywhere
    assert channels["total_orders"] == 3
    assert channels["total_gross_cents"] == 65000
    assert channels["total_refunds_cents"] == 5000
    assert channels["total_net_cents"] == 60000


async def test_second_tenants_orders_never_appear(db, test_business: Business):
    """Tenant isolation: business_id is sourced only from the authenticated
    user, and a second tenant's sale must never leak into the first's totals
    (or vice versa)."""
    other_business = await _make_business(db)
    try:
        owner_1, coke, _burger, _chips = await _pos_setup(db, test_business)
        owner_2, coke_2, _b2, _c2 = await _pos_setup(db, other_business)

        await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=coke.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=20000,
            ),
            user=owner_1, db=db,
        )
        # A much larger sale on the OTHER tenant -- if isolation is broken
        # this would swamp business 1's totals and the assertions below
        # would fail loudly rather than silently.
        await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=coke_2.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=20000,
            ),
            user=owner_2, db=db,
        )

        summary_1 = await get_summary(period="today", user=owner_1, db=db)
        summary_2 = await get_summary(period="today", user=owner_2, db=db)
        assert summary_1.gross_cents == 20000
        assert summary_1.completed_orders == 1
        assert summary_2.gross_cents == 20000
        assert summary_2.completed_orders == 1
    finally:
        await _cleanup_business(db, other_business)


async def test_sale_just_after_local_midnight_lands_on_the_correct_local_day(db, test_business: Business):
    """
    BUG-0043: daily bucketing used to `cast(Order.created_at, Date)` -- a
    raw UTC-date cast. Africa/Johannesburg is UTC+2, so a sale at local
    00:30 is still 22:30 the PREVIOUS day in UTC. Assert it lands on
    TODAY's local bucket, not yesterday's.
    """
    tz_name = test_business.timezone
    today_local = today_date_for_business(tz_name)
    local_midnight_utc = local_midnight_to_utc(today_local, tz_name)
    sale_at_0030_local = local_midnight_utc + timedelta(minutes=30)

    order = Order(
        business_id=test_business.id, order_number="WA-TEST-MIDNIGHT", status="COLLECTED",
        payment_status="PAID", order_mode="PICKUP", source="WHATSAPP",
        subtotal_cents=1000, total_cents=1000, currency="ZAR",
        created_at=sale_at_0030_local,
    )
    db.add(order)
    await db.commit()

    owner = _owner(test_business.id)
    daily = await get_daily_breakdown(days=2, user=owner, db=db)
    by_day = {row.day: row for row in daily}

    assert by_day[today_local].gross_cents == 1000, (
        "a sale at 00:30 local time must be bucketed on TODAY's local date, "
        f"got buckets: { {d: r.gross_cents for d, r in by_day.items()} }"
    )
    yesterday_local = today_local - timedelta(days=1)
    if yesterday_local in by_day:
        assert by_day[yesterday_local].gross_cents == 0, (
            "the 00:30-local sale must NOT be counted on the previous UTC day"
        )


async def test_imported_order_counts_once_with_right_channel_and_vat_sums(db, test_business: Business):
    """
    An imported Uber Eats order's `created_at` is IMPORT time, not sale
    time (external_orders/service.py never overrides TimestampMixin's
    default) -- give it a created_at far outside the reporting window and
    an external_placed_at inside it, and confirm it still counts, exactly
    once, under the UBER_EATS channel. Also confirms vat_cents sums the
    stored tax_cents of every counted sale, from a mix of a real
    VAT-computed POS sale and a manually-set imported order.
    """
    await update_business_settings(
        body=BusinessSettingsUpdate(vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=True),
        user=_owner(test_business.id), db=db,
    )
    owner, coke, _burger, _chips = await _pos_setup(db, test_business)

    pos_sale = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=coke.id, quantity=1)],
            payment_method="CASH", cash_tendered_cents=20000,
        ),
        user=owner, db=db,
    )
    assert pos_sale.tax_cents > 0  # VAT-registered business -> real, non-zero VAT

    now = today_date_for_business(test_business.timezone)
    placed_at = local_midnight_to_utc(now, test_business.timezone) + timedelta(hours=10)
    imported_at = placed_at + timedelta(days=3)  # imported 3 days later
    imported_order = Order(
        business_id=test_business.id, order_number="UBER-TEST-1", status="COMPLETED",
        payment_status="PAID", order_mode="DELIVERY", source="UBER_EATS",
        subtotal_cents=14000, total_cents=14000, tax_cents=1826, currency="ZAR",
        external_order_id="uber-ext-1", external_placed_at=placed_at,
        external_imported_at=imported_at, created_at=imported_at,
    )
    db.add(imported_order)
    await db.commit()

    channels = await channel_breakdown(days=1, user=owner, db=db)
    by_source = {c["source"]: c for c in channels["channels"]}
    assert by_source["UBER_EATS"]["order_count"] == 1
    assert by_source["UBER_EATS"]["gross_cents"] == 14000

    summary = await get_summary(period="today", user=owner, db=db)
    assert summary.vat_cents == pos_sale.tax_cents + 1826
    assert summary.gross_cents == pos_sale.total_cents + 14000
