"""
Real-DB tests for Sales Wallets (backend/app/analytics/sales_wallets.py).

A mocked session cannot prove SQL grouping, timezone range boundaries or
tenant isolation, so these run against the throwaway Postgres (TESTING.md):

  TEST_DATABASE_URL=postgresql+asyncpg://nextgen_test:...@127.0.0.1:55787/nextgen_test \
    python -m pytest tests/integration/test_sales_wallets_realdb.py -q
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from backend.app.analytics.sales_wallets import resolve_range, wallet_for_source
from backend.app.api.v1.routes_analytics import get_sales_wallet_orders, get_sales_wallets, get_summary
from backend.app.api.v1.routes_external_orders import channel_breakdown
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order
from shared.utils import generate_business_code
from shared.utils.time import local_midnight_to_utc, today_date_for_business

TZ = "Africa/Johannesburg"


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _business(db, *, advanced: bool = True) -> uuid.UUID:
    unique = uuid.uuid4().hex[:8]
    b = Business(name=f"Wallet Test {unique}", slug=f"wallet-test-{unique}", business_code=generate_business_code(),
                 currency="ZAR", timezone=TZ, advanced_analytics_enabled=advanced)
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b.id  # read before any later commit expires the instance


async def _cleanup(db, *businesses) -> None:
    await db.rollback()  # a failed assertion mid-query must not block the cleanup
    for bid in businesses:
        await db.execute(delete(Order).where(Order.business_id == bid))
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    await db.commit()


def _at_local(day: date, hour: int) -> datetime:
    return local_midnight_to_utc(day, TZ) + timedelta(hours=hour)


def _order(business_id: uuid.UUID, n: int, *, source: str, total: int, status: str = "COMPLETED",
           payment_status: str = "PAID", refund: int | None = None, created: datetime | None = None,
           placed: datetime | None = None, tax: int = 0) -> Order:
    o = Order(business_id=business_id, order_number=f"T-{n:04d}", source=source, status=status,
              payment_status=payment_status, subtotal_cents=total, total_cents=total, tax_cents=tax,
              refund_amount_cents=refund, external_placed_at=placed,
              payment_method="EXTERNAL_PLATFORM" if source in ("UBER_EATS", "MR_D") else "CASH")
    if created is not None:
        o.created_at = created
    return o


@pytest.fixture
async def scenario(db):
    """
    Restaurant A, today (local): Uber R200 + Uber R100 (R30 refunded), Mr D R150, POS R300, WhatsApp R80
    (cash on collection, COLLECTED), POS voided R50, WhatsApp CANCELLED R40, WhatsApp unpaid NEW R60.
    Yesterday: Uber R999 (placed yesterday, imported today; must count as yesterday).
    Restaurant B: Uber R5000 today (must never appear in A's figures).
    """
    a, b = await _business(db), await _business(db)
    today = today_date_for_business(TZ)
    now_a = _at_local(today, 0) + timedelta(minutes=30)   # just after local midnight: the BUG-0032 boundary
    yday = today - timedelta(days=1)
    db.add_all([
        _order(a, 1, source="UBER_EATS", total=20000, placed=now_a, tax=2609),
        _order(a, 2, source="UBER_EATS", total=10000, placed=now_a, refund=3000, payment_status="PARTIALLY_REFUNDED"),
        _order(a, 3, source="MR_D", total=15000, placed=now_a),
        _order(a, 4, source="POS", total=30000, created=now_a),
        _order(a, 5, source="WHATSAPP", total=8000, status="COLLECTED", payment_status="CASH_ON_COLLECTION", created=now_a),
        _order(a, 6, source="POS", total=5000, status="CANCELLED", payment_status="VOIDED", created=now_a),
        _order(a, 7, source="WHATSAPP", total=4000, status="CANCELLED", payment_status="PENDING", created=now_a),
        _order(a, 8, source="WHATSAPP", total=6000, status="NEW", payment_status="PENDING", created=now_a),
        _order(a, 9, source="UBER_EATS", total=99900, placed=_at_local(yday, 13), created=now_a),
        _order(b, 1, source="UBER_EATS", total=500000, placed=now_a),
    ])
    await db.commit()
    yield a, b
    await _cleanup(db, a, b)


def _by_key(resp) -> dict:
    return {w["key"]: w for w in resp["wallets"]}


async def test_wallet_figures_total_and_no_double_count(db, scenario):
    a, _ = scenario
    resp = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(a), db=db)
    w = _by_key(resp)
    assert (w["UBER_EATS"]["order_count"], w["UBER_EATS"]["gross_cents"], w["UBER_EATS"]["refunds_cents"],
            w["UBER_EATS"]["net_cents"]) == (2, 30000, 3000, 27000)
    assert (w["MR_D"]["order_count"], w["MR_D"]["gross_cents"], w["MR_D"]["net_cents"]) == (1, 15000, 15000)
    # POS R300 + WhatsApp cash-on-collection R80; the void, the cancellation and the unpaid NEW are not sales.
    assert (w["GENERAL"]["order_count"], w["GENERAL"]["gross_cents"]) == (2, 38000)
    assert (w["GENERAL"]["voided_count"], w["GENERAL"]["voided_cents"]) == (1, 5000)
    t = resp["total"]
    for field in ("order_count", "gross_cents", "refunds_cents", "net_cents", "vat_cents", "voided_count", "voided_cents"):
        assert t[field] == sum(x[field] for x in resp["wallets"]), field
    assert (t["order_count"], t["gross_cents"], t["net_cents"]) == (5, 83000, 80000)
    assert w["UBER_EATS"]["payment_status_counts"] == {"PAID": 1, "PARTIALLY_REFUNDED": 1}
    assert w["UBER_EATS"]["vat_cents"] == 2609


async def test_wallets_agree_with_existing_summary_and_channel_reports(db, scenario):
    a, _ = scenario
    owner = _owner(a)
    wallets = await get_sales_wallets(range_="today", start=None, end=None, user=owner, db=db)
    summary = await get_summary(period="today", user=owner, db=db)
    assert (wallets["total"]["gross_cents"], wallets["total"]["refunds_cents"], wallets["total"]["net_cents"]) == (
        summary.gross_cents, summary.refunds_cents, summary.net_cents)
    channels = await channel_breakdown(days=1, user=owner, db=db)
    uber_channel = next(c for c in channels["channels"] if c["source"] == "UBER_EATS")
    # /channels uses a rolling 24 h window, so it may also include yesterday's late-afternoon Uber order;
    # the wallets never exceed it and agree exactly on today's orders.
    assert _by_key(wallets)["UBER_EATS"]["gross_cents"] <= uber_channel["gross_cents"]


async def test_sale_time_not_import_time_and_yesterday_range(db, scenario):
    a, _ = scenario
    today = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(a), db=db)
    assert all(o != 99900 for o in [_by_key(today)["UBER_EATS"]["gross_cents"]])
    yday = await get_sales_wallets(range_="yesterday", start=None, end=None, user=_owner(a), db=db)
    assert (_by_key(yday)["UBER_EATS"]["order_count"], _by_key(yday)["UBER_EATS"]["gross_cents"]) == (1, 99900)
    week = await get_sales_wallets(range_="7d", start=None, end=None, user=_owner(a), db=db)
    assert _by_key(week)["UBER_EATS"]["gross_cents"] == 30000 + 99900


async def test_tenant_isolation(db, scenario):
    a, b = scenario
    resp_a = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(a), db=db)
    resp_b = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(b), db=db)
    assert 500000 not in [w["gross_cents"] for w in resp_a["wallets"]]
    assert (resp_b["total"]["order_count"], resp_b["total"]["gross_cents"]) == (1, 500000)
    detail_b = await get_sales_wallet_orders(wallet="TOTAL", range_="7d", start=None, end=None, page=1, page_size=100,
                                             user=_owner(b), db=db)
    assert {o["order_number"] for o in detail_b["orders"]} == {"T-0001"} and detail_b["total_count"] == 1


async def test_breakdown_lists_only_that_wallets_sales_and_pages(db, scenario):
    a, _ = scenario
    owner = _owner(a)
    general = await get_sales_wallet_orders(wallet="GENERAL", range_="today", start=None, end=None, page=1,
                                            page_size=25, user=owner, db=db)
    assert {o["order_number"] for o in general["orders"]} == {"T-0004", "T-0005"}
    assert {s["source"]: s["gross_cents"] for s in general["by_source"]} == {"POS": 30000, "WHATSAPP": 8000}
    assert general["summary"]["gross_cents"] == 38000 and general["summary"]["platform_fees"] is None
    uber = await get_sales_wallet_orders(wallet="UBER_EATS", range_="today", start=None, end=None, page=1,
                                         page_size=1, user=owner, db=db)
    assert uber["total_count"] == 2 and len(uber["orders"]) == 1
    page2 = await get_sales_wallet_orders(wallet="UBER_EATS", range_="today", start=None, end=None, page=2,
                                          page_size=1, user=owner, db=db)
    assert {uber["orders"][0]["id"], page2["orders"][0]["id"]} and uber["orders"][0]["id"] != page2["orders"][0]["id"]
    refunded = next(o for o in uber["orders"] + page2["orders"] if o["order_number"] == "T-0002")
    assert (refunded["gross_cents"], refunded["refund_cents"], refunded["net_cents"]) == (10000, 3000, 7000)
    total = await get_sales_wallet_orders(wallet="TOTAL", range_="today", start=None, end=None, page=1,
                                          page_size=100, user=owner, db=db)
    assert total["total_count"] == 5 and len({o["id"] for o in total["orders"]}) == 5


async def test_fees_and_settlement_are_never_invented(db, scenario):
    a, _ = scenario
    w = _by_key(await get_sales_wallets(range_="today", start=None, end=None, user=_owner(a), db=db))
    for key in ("UBER_EATS", "MR_D"):
        assert w[key]["platform_fees"]["available"] is False and "amount_cents" not in w[key]["platform_fees"]
        assert w[key]["settlement"]["available"] is False and "amount_cents" not in w[key]["settlement"]


async def test_custom_range_and_validation(db, scenario):
    a, _ = scenario
    today = today_date_for_business(TZ)
    resp = await get_sales_wallets(range_="custom", start=today - timedelta(days=1), end=today, user=_owner(a), db=db)
    assert resp["start_date"] == (today - timedelta(days=1)).isoformat()
    assert _by_key(resp)["UBER_EATS"]["gross_cents"] == 30000 + 99900
    for start, end in ((today, today - timedelta(days=1)), (None, today), (today - timedelta(days=400), today)):
        with pytest.raises(AppError) as exc:
            await get_sales_wallets(range_="custom", start=start, end=end, user=_owner(a), db=db)
        assert exc.value.status_code == 422


async def test_available_without_advanced_analytics(db):
    """Owner decision 2026-11-11: the wallets are on every plan's standard dashboard."""
    b = await _business(db, advanced=False)
    try:
        db.add(_order(b, 1, source="UBER_EATS", total=12_300))
        await db.commit()
        result = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(b), db=db)
        assert result["total"]["net_cents"] == 12_300
        detail = await get_sales_wallet_orders(wallet="UBER_EATS", range_="today", start=None, end=None, page=1,
                                               page_size=25, user=_owner(b), db=db)
        assert detail["total_count"] == 1
        with pytest.raises(AppError) as exc:
            await get_sales_wallet_orders(wallet="NOPE", range_="today", start=None, end=None, page=1, page_size=25,
                                          user=_owner(b), db=db)
        assert exc.value.status_code == 404
    finally:
        await _cleanup(db, b)


def test_every_source_maps_to_exactly_one_wallet():
    from shared.enums import OrderSource
    assert {s.value: wallet_for_source(s.value) for s in OrderSource} == {
        **{s.value: "GENERAL" for s in OrderSource}, "UBER_EATS": "UBER_EATS", "MR_D": "MR_D"}


def test_named_ranges():
    d = date(2026, 11, 11)
    assert (resolve_range("month", None, None, TZ, today=d).start_date, resolve_range("month", None, None, TZ, today=d).end_date) == (date(2026, 11, 1), d)
    assert resolve_range("7d", None, None, TZ, today=d).start_date == date(2026, 11, 5)
    r = resolve_range("yesterday", None, None, TZ, today=d)
    assert r.start_date == r.end_date == date(2026, 11, 10)
    assert r.start_utc == datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
