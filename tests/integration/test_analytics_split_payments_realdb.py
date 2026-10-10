"""
Real-DB proof that the payment-mix report (routes_analytics.py::
get_payment_method_breakdown) buckets MONEY BY ALLOCATION for split POS
sales (migration 0055, docs/SPLIT_PAYMENTS_CONTRACT.md) while a legacy
order (zero POSPaymentAllocation rows) is bucketed exactly as before.

The checkout-side code that creates allocation rows is being built in
parallel and does not exist yet in this branch, so split orders here are
constructed directly (Order + POSPaymentAllocation rows inserted straight
into the real test DB) rather than via routes_pos.py::create_pos_order —
this file tests the READ side against the already-migrated schema, per
this task's own brief.

Requires TEST_DATABASE_URL (see tests/integration/conftest.py / TESTING.md):

  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
  python -m pytest tests/integration/test_analytics_split_payments_realdb.py -q
"""
import uuid

import pytest

from backend.app.api.v1.routes_analytics import get_payment_method_breakdown
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.order import Order
from shared.models.pos_payment import POSPaymentAllocation


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _sale_order(business_id: uuid.UUID, order_number: str, total_cents: int, tax_cents: int = 0, **overrides) -> Order:
    defaults = dict(
        business_id=business_id, order_number=order_number,
        status="COMPLETED", payment_status="PAID", order_mode="DINE_IN", source="POS",
        subtotal_cents=total_cents, total_cents=total_cents, tax_cents=tax_cents,
        currency="ZAR",
    )
    defaults.update(overrides)
    return Order(**defaults)


async def _run(db, business: Business):
    business.advanced_analytics_enabled = True
    return await get_payment_method_breakdown(days=1, user=_owner(business.id), db=db)


async def test_legacy_single_cash_order_unchanged(db, test_business: Business):
    """Regression: an order with zero allocation rows buckets exactly as
    it always has, by Order.payment_method/total_cents."""
    order = _sale_order(test_business.id, "SPLIT-LEGACY-1", 20000, payment_method="CASH")
    db.add(order)
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert set(by_method) == {"CASH"}
    assert by_method["CASH"].order_count == 1
    assert by_method["CASH"].gross_cents == 20000
    assert by_method["CASH"].net_cents == 20000
    assert report.total_orders == 1
    assert report.total_gross_cents == 20000


async def test_legacy_full_refund_nets_out_exactly_as_before(db, test_business: Business):
    order = _sale_order(
        test_business.id, "SPLIT-LEGACY-REFUND", 20000, payment_method="CARD",
        payment_status="REFUNDED", refund_amount_cents=20000,
    )
    db.add(order)
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert by_method["CARD"].gross_cents == 20000
    assert by_method["CARD"].refunds_cents == 20000
    assert by_method["CARD"].net_cents == 0


async def test_two_way_split_cash_and_card_buckets_separately(db, test_business: Business):
    """Cash R150 + Card R150 = R300 order. Never a fake 'SPLIT' bucket."""
    order = _sale_order(test_business.id, "SPLIT-2WAY", 30000, payment_method="SPLIT")
    db.add(order)
    await db.flush()
    db.add_all([
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CASH", amount_cents=15000,
            cash_tendered_cents=20000, change_due_cents=5000,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CARD", amount_cents=15000, payment_reference="term-1",
        ),
    ])
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert "SPLIT" not in by_method
    assert by_method["CASH"].gross_cents == 15000
    assert by_method["CARD"].gross_cents == 15000
    assert report.total_gross_cents == 30000
    assert report.total_orders == 2  # one allocation-row per bucket, by design


async def test_three_way_split_cash_card_pay_by_link_buckets_separately(db, test_business: Business):
    order = _sale_order(test_business.id, "SPLIT-3WAY", 30000, payment_method="SPLIT")
    db.add(order)
    await db.flush()
    db.add_all([
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CASH", amount_cents=10000,
            cash_tendered_cents=10000, change_due_cents=0,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CARD", amount_cents=10000,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="PAY_BY_LINK", amount_cents=10000, provider="YOCO",
        ),
    ])
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert set(by_method) == {"CASH", "CARD", "PAY_BY_LINK"}
    assert by_method["CASH"].gross_cents == 10000
    assert by_method["CARD"].gross_cents == 10000
    assert by_method["PAY_BY_LINK"].gross_cents == 10000
    assert report.total_gross_cents == 30000


async def test_split_order_partial_refund_cash_exact_non_cash_proportional(db, test_business: Business):
    """
    Worked example (matches this task's brief exactly): Cash R150 + Card
    R150 = R300 order. A R100 refund with refund_cash_cents=40 (exact,
    manager-recorded) -> CASH bucket refund = R40. The remaining R60 has
    only ONE non-cash allocation (Card, 100% of the non-cash total) -> Card
    bucket refund = R60 (40 + 60 == 100, the real total refund).
    """
    order = _sale_order(
        test_business.id, "SPLIT-REFUND-2WAY", 30000, payment_method="SPLIT",
        payment_status="PARTIALLY_REFUNDED", refund_amount_cents=10000, refund_cash_cents=4000,
    )
    db.add(order)
    await db.flush()
    db.add_all([
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CASH", amount_cents=15000,
            cash_tendered_cents=15000, change_due_cents=0,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CARD", amount_cents=15000,
        ),
    ])
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert by_method["CASH"].refunds_cents == 4000
    assert by_method["CARD"].refunds_cents == 6000
    assert by_method["CASH"].net_cents == 15000 - 4000
    assert by_method["CARD"].net_cents == 15000 - 6000
    assert report.total_refunds_cents == 10000  # 4000 + 6000, matches the real refund exactly


async def test_split_order_partial_refund_spreads_across_two_non_cash_allocations(db, test_business: Business):
    """
    3-way split: Cash R100 + Card R100 + Pay by Link R100 = R300. A R150
    refund with refund_cash_cents=30 (exact) -> CASH refund = R30. Remaining
    R120 spreads proportionally across Card/Pay-by-Link, each 50% of the
    R200 non-cash total -> R60 each (30 + 60 + 60 == 150).
    """
    order = _sale_order(
        test_business.id, "SPLIT-REFUND-3WAY", 30000, payment_method="SPLIT",
        payment_status="PARTIALLY_REFUNDED", refund_amount_cents=15000, refund_cash_cents=3000,
    )
    db.add(order)
    await db.flush()
    db.add_all([
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CASH", amount_cents=10000,
            cash_tendered_cents=10000, change_due_cents=0,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="CARD", amount_cents=10000,
        ),
        POSPaymentAllocation(
            business_id=test_business.id, order_id=order.id,
            payment_method="PAY_BY_LINK", amount_cents=10000, provider="PAYFAST",
        ),
    ])
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert by_method["CASH"].refunds_cents == 3000
    assert by_method["CARD"].refunds_cents == 6000
    assert by_method["PAY_BY_LINK"].refunds_cents == 6000
    assert report.total_refunds_cents == 15000


async def test_tenant_isolation_split_orders_never_leak_across_businesses(db, test_business: Business):
    """A second tenant's split order must never appear in this tenant's report."""
    from shared.utils import generate_business_code

    other = Business(
        name="Other Tenant", slug=f"other-{uuid.uuid4().hex[:8]}",
        business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
    )
    db.add(other)
    await db.commit()
    await db.refresh(other)

    other_order = _sale_order(other.id, "OTHER-SPLIT", 30000, payment_method="SPLIT")
    db.add(other_order)
    await db.flush()
    db.add(POSPaymentAllocation(
        business_id=other.id, order_id=other_order.id,
        payment_method="CARD", amount_cents=30000,
    ))

    mine = _sale_order(test_business.id, "MINE-CASH", 5000, payment_method="CASH")
    db.add(mine)
    await db.commit()

    report = await _run(db, test_business)
    by_method = {m.payment_method: m for m in report.methods}
    assert set(by_method) == {"CASH"}
    assert report.total_gross_cents == 5000

    from sqlalchemy import delete
    await db.execute(delete(POSPaymentAllocation).where(POSPaymentAllocation.business_id == other.id))
    await db.execute(delete(Order).where(Order.business_id == other.id))
    await db.execute(delete(Business).where(Business.id == other.id))
    await db.commit()
