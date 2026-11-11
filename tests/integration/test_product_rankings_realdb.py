"""
Real-DB tests for best-selling products (backend/app/analytics/product_rankings.py).

All data here is constructed test data, not sales. A mocked session cannot
prove SQL grouping, refund attribution, date boundaries or tenant isolation,
so these run against the throwaway Postgres (TESTING.md):

  TEST_DATABASE_URL=postgresql+asyncpg://nextgen_test:...@127.0.0.1:55787/nextgen_test \
    python -m pytest tests/integration/test_product_rankings_realdb.py -q
"""
import uuid
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.routes_analytics import get_best_sellers, get_product_performance, get_sales_wallets
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.delivery import DeliveryOrder
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderItem
from shared.utils import generate_business_code
from shared.utils.time import local_midnight_to_utc, today_date_for_business

TZ = "Africa/Johannesburg"


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _business(db) -> uuid.UUID:
    unique = uuid.uuid4().hex[:8]
    b = Business(name=f"Ranking Test {unique}", slug=f"ranking-test-{unique}", business_code=generate_business_code(),
                 currency="ZAR", timezone=TZ, advanced_analytics_enabled=False)
    db.add(b)
    await db.commit()
    return b.id


async def _menu(db, business_id: uuid.UUID, *names: str) -> dict[str, uuid.UUID]:
    items = {n: MenuItem(business_id=business_id, name=n, price_cents=1000, image_url=f"https://img.test/{n}.png")
             for n in names}
    db.add_all(items.values())
    await db.flush()
    return {n: i.id for n, i in items.items()}


async def _cleanup(db, *businesses) -> None:
    await db.rollback()
    for bid in businesses:
        await db.execute(delete(DeliveryOrder).where(DeliveryOrder.business_id == bid))
        await db.execute(delete(OrderItem).where(OrderItem.business_id == bid))
        await db.execute(delete(Order).where(Order.business_id == bid))
        await db.execute(delete(MenuItem).where(MenuItem.business_id == bid))
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    await db.commit()


def _at_local(day: date, hour: int) -> datetime:
    return local_midnight_to_utc(day, TZ) + timedelta(hours=hour)


def _sale(db, business_id, n, *, source, total, lines, at, status="COMPLETED", payment_status="PAID",
          refund=None, external_order_id=None):
    """One order and its lines. lines: (menu_item_id | None, name, qty, line_total, option_name | None)."""
    external = source in ("UBER_EATS", "MR_D")
    o = Order(business_id=business_id, order_number=f"R-{n:04d}", source=source, status=status,
              payment_status=payment_status, subtotal_cents=total, total_cents=total, tax_cents=0,
              refund_amount_cents=refund, payment_method="EXTERNAL_PLATFORM" if external else "CASH",
              external_order_id=external_order_id, external_placed_at=at if external else None)
    if not external:
        o.created_at = at
    db.add(o)
    o.items = [
        OrderItem(business_id=business_id, menu_item_id=mid, name_snapshot=name, quantity=qty,
                  unit_price_cents=line // qty, line_total_cents=line, base_price_cents=line // qty,
                  selected_options_snapshot=[{"option_name": opt}] if opt else None)
        for mid, name, qty, line, opt in lines
    ]
    return o


@pytest.fixture
async def scenario(db):
    """
    Restaurant A, today (test data):
      Uber U1: 2 x Burger (Large) R200 + 1 x Fries R30            total R250 (incl. a R20 fee)
      Uber U2: 1 x Burger R100, FULLY refunded
      Uber U3: 3 x Fries R90, PARTIALLY refunded R20
      Mr D M1: 1 x Shake R50 + 1 x Wings R70
      POS  P1: 2 x Wings (Hot) R140 + 2 x Wings (Mild) R140 + 1 x ad hoc "Special" R15 (no product)
      POS  P2: 10 x Burger, VOIDED             -> never counts
      WhatsApp W1: 10 x Shake, CANCELLED      -> never counts
      Uber yesterday: 5 x Shake R250          -> only in 7d
      One live Uber order held in NEEDS_MAPPING (no sale exists)
    Restaurant B: Uber 50 x Burger, one line even pointing at A's Burger id; must never reach A.
    """
    a, b = await _business(db), await _business(db)
    ma = await _menu(db, a, "Burger", "Fries", "Shake", "Wings")
    mb = await _menu(db, b, "Burger")
    today = today_date_for_business(TZ)
    now = _at_local(today, 0) + timedelta(minutes=30)   # just after local midnight
    yday = _at_local(today - timedelta(days=1), 13)
    _sale(db, a, 1, source="UBER_EATS", total=25000, at=now, external_order_id="u1",
          lines=[(ma["Burger"], "Burger", 2, 20000, "Large"), (ma["Fries"], "Fries", 1, 3000, None)])
    _sale(db, a, 2, source="UBER_EATS", total=10000, at=now, refund=10000, payment_status="REFUNDED",
          external_order_id="u2", lines=[(ma["Burger"], "Burger", 1, 10000, None)])
    _sale(db, a, 3, source="UBER_EATS", total=9000, at=now, refund=2000, payment_status="PARTIALLY_REFUNDED",
          external_order_id="u3", lines=[(ma["Fries"], "Fries", 3, 9000, None)])
    _sale(db, a, 4, source="MR_D", total=12000, at=now, external_order_id="m1",
          lines=[(ma["Shake"], "Shake", 1, 5000, None), (ma["Wings"], "Wings", 1, 7000, None)])
    _sale(db, a, 5, source="POS", total=29500, at=now,
          lines=[(ma["Wings"], "Wings", 2, 14000, "Hot"), (ma["Wings"], "Wings", 2, 14000, "Mild"),
                 (None, "Special", 1, 1500, None)])
    _sale(db, a, 6, source="POS", total=100000, at=now, status="CANCELLED", payment_status="VOIDED",
          lines=[(ma["Burger"], "Burger", 10, 100000, None)])
    _sale(db, a, 7, source="WHATSAPP", total=50000, at=now, status="CANCELLED", payment_status="PENDING",
          lines=[(ma["Shake"], "Shake", 10, 50000, None)])
    _sale(db, a, 8, source="UBER_EATS", total=25000, at=yday, external_order_id="u-yday",
          lines=[(ma["Shake"], "Shake", 5, 25000, None)])
    db.add(DeliveryOrder(business_id=a, provider="UBER_EATS", external_order_id="held-1",
                         external_store_id="store-a", status="NEEDS_MAPPING", placed_at=now))
    _sale(db, b, 1, source="UBER_EATS", total=500000, at=now, external_order_id="u1",
          lines=[(mb["Burger"], "Burger", 49, 490000, None), (ma["Burger"], "Burger", 1, 10000, None)])
    await db.commit()
    yield a, b, ma, mb
    await _cleanup(db, a, b)


async def _rankings(db, business_id, rank_by="quantity", range_="today"):
    resp = await get_best_sellers(range_=range_, start=None, end=None, rank_by=rank_by, user=_owner(business_id), db=db)
    return resp, {c["key"]: c for c in resp["channels"]}


def _names(channel) -> list[str]:
    return [r["name"] for r in channel["ranking"]]


async def test_top3_per_channel_by_quantity(db, scenario):
    a, *_ = scenario
    resp, ch = await _rankings(db, a)
    assert [c["key"] for c in resp["channels"]] == ["UBER_EATS", "MR_D", "GENERAL", "ALL"]
    assert _names(ch["UBER_EATS"]) == ["Fries", "Burger"]
    assert [r["rank"] for r in ch["UBER_EATS"]["ranking"]] == [1, 2]
    assert _names(ch["MR_D"]) == ["Wings", "Shake"]          # 1 each: tie broken by revenue (R70 > R50)
    assert _names(ch["GENERAL"]) == ["Wings"]
    assert _names(ch["ALL"]) == ["Wings", "Fries", "Burger"]  # Shake (1 today) is 4th
    wings = ch["ALL"]["ranking"][0]
    assert (wings["quantity"], wings["gross_cents"]) == (5, 35000)
    assert wings["image_url"] == "https://img.test/Wings.png"


async def test_top3_by_revenue(db, scenario):
    a, *_ = scenario
    _, ch = await _rankings(db, a, rank_by="revenue")
    assert _names(ch["UBER_EATS"]) == ["Burger", "Fries"]
    assert _names(ch["ALL"]) == ["Wings", "Burger", "Fries"]


async def test_quantities_revenue_and_shares(db, scenario):
    a, *_ = scenario
    _, ch = await _rankings(db, a)
    uber = {r["name"]: r for r in ch["UBER_EATS"]["ranking"]}
    assert (uber["Burger"]["quantity"], uber["Burger"]["gross_cents"]) == (3, 30000)   # 2 + 1, sizes merged
    assert (uber["Fries"]["quantity"], uber["Fries"]["gross_cents"]) == (4, 12000)
    assert (ch["UBER_EATS"]["total_quantity"], ch["UBER_EATS"]["total_item_gross_cents"]) == (7, 42000)
    assert uber["Fries"]["quantity_share_pct"] == 57.1
    assert uber["Burger"]["revenue_share_pct"] == 71.4
    # ALL is the same rows once: 14 units, R835 of item sales (R20 Uber fee is not an item sale)
    assert (ch["ALL"]["total_quantity"], ch["ALL"]["total_item_gross_cents"]) == (14, 83500)


async def test_refunds_attributed_only_when_accurate(db, scenario):
    a, *_ = scenario
    _, ch = await _rankings(db, a)
    uber = {r["name"]: r for r in ch["UBER_EATS"]["ranking"]}
    # Full refund of U2 belongs entirely to its one Burger line
    assert uber["Burger"]["refund_adjusted_cents"] == 20000 and not uber["Burger"]["has_unattributed_refund"]
    # U3's partial R20 refund is not allocated to Fries; it is disclosed instead
    assert uber["Fries"]["refund_adjusted_cents"] is None and uber["Fries"]["has_unattributed_refund"]
    assert ch["UBER_EATS"]["unattributed_refunds"] == {"order_count": 1, "refunds_cents": 2000}
    assert ch["MR_D"]["unattributed_refunds"] == {"order_count": 0, "refunds_cents": 0}


async def test_cancelled_and_voided_never_rank(db, scenario):
    a, *_ = scenario
    _, ch = await _rankings(db, a)
    assert all(r["name"] != "Burger" or r["quantity"] == 3 for r in ch["ALL"]["ranking"])  # not +10 voided
    assert "Shake" not in _names(ch["GENERAL"])                                         # cancelled WhatsApp


async def test_unmapped_lines_and_held_orders_are_disclosed_not_assigned(db, scenario):
    a, *_ = scenario
    _, ch = await _rankings(db, a)
    assert ch["GENERAL"]["unmapped"] == {"quantity": 1, "gross_cents": 1500, "names": ["Special"]}
    assert ch["GENERAL"]["total_quantity"] == 5                       # counted in the total...
    assert ch["GENERAL"]["ranking"][0]["quantity_share_pct"] == 80.0  # ...so shares stay honest
    assert ch["UBER_EATS"]["awaiting_mapping_orders"] == 1
    assert ch["ALL"]["awaiting_mapping_orders"] == 1
    assert ch["MR_D"]["awaiting_mapping_orders"] == 0


async def test_date_filtering(db, scenario):
    a, *_ = scenario
    _, today = await _rankings(db, a)
    _, yday = await _rankings(db, a, range_="yesterday")
    _, week = await _rankings(db, a, range_="7d")
    assert "Shake" not in _names(today["UBER_EATS"])
    assert _names(yday["UBER_EATS"]) == ["Shake"] and yday["MR_D"]["ranking"] == []
    assert _names(week["UBER_EATS"])[0] == "Shake"        # 5 units yesterday + nothing today
    assert week["ALL"]["total_quantity"] == 19


async def test_tenant_isolation(db, scenario):
    a, b, ma, mb = scenario
    _, ch = await _rankings(db, a)
    assert {r["name"]: r["quantity"] for r in ch["UBER_EATS"]["ranking"]}["Burger"] == 3   # not B's 50
    _, chb = await _rankings(db, b)
    assert [(r["menu_item_id"], r["quantity"]) for r in chb["ALL"]["ranking"]] == [(str(mb["Burger"]), 49), (str(ma["Burger"]), 1)]
    assert chb["ALL"]["awaiting_mapping_orders"] == 0
    # B asking about A's product sees only B's own line, never A's 3 Burgers
    perf = await get_product_performance(menu_item_id=ma["Burger"], range_="today", start=None, end=None, user=_owner(b), db=db)
    assert perf["total_quantity"] == 1 and perf["image_url"] is None
    with pytest.raises(AppError) as exc:
        await get_product_performance(menu_item_id=ma["Shake"], range_="today", start=None, end=None, user=_owner(b), db=db)
    assert exc.value.status_code == 404


async def test_same_marketplace_order_cannot_be_stored_twice(db, scenario):
    """The write-time guard rankings rely on: a repeated webhook or a re-imported report row cannot create a second sale."""
    a, *_ = scenario
    _sale(db, a, 99, source="UBER_EATS", total=25000, at=datetime.now(), external_order_id="u1",
          lines=[(None, "dup", 1, 25000, None)])
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_product_performance_across_channels(db, scenario):
    a, _, ma, _ = scenario
    perf = await get_product_performance(menu_item_id=ma["Wings"], range_="7d", start=None, end=None, user=_owner(a), db=db)
    channels = {c["key"]: c for c in perf["channels"]}
    assert (channels["MR_D"]["quantity"], channels["MR_D"]["gross_cents"]) == (1, 7000)
    assert (channels["GENERAL"]["quantity"], channels["GENERAL"]["gross_cents"]) == (4, 28000)
    assert channels["UBER_EATS"]["quantity"] == 0
    assert channels["GENERAL"]["quantity_share_pct"] == 80.0 and channels["MR_D"]["revenue_share_pct"] == 20.0
    assert {v["label"]: v["quantity"] for v in perf["variants"]} == {"Hot": 2, "Mild": 2, "Standard": 1}
    assert len(perf["daily"]) == 7
    assert perf["daily"][-1]["general"] == {"quantity": 4, "gross_cents": 28000}
    assert sum(d["mr_d"]["quantity"] for d in perf["daily"]) == 1


async def test_empty_restaurant_has_no_fictional_rankings(db):
    b = await _business(db)
    try:
        _, ch = await _rankings(db, b)
        assert all(c["ranking"] == [] and c["total_quantity"] == 0 for c in ch.values())
        with pytest.raises(AppError) as exc:
            await get_product_performance(menu_item_id=uuid.uuid4(), range_="today", start=None, end=None,
                                          user=_owner(b), db=db)
        assert exc.value.status_code == 404
    finally:
        await _cleanup(db, b)


async def test_rankings_and_wallets_agree_on_which_orders_count(db, scenario):
    a, *_ = scenario
    wallets = await get_sales_wallets(range_="today", start=None, end=None, user=_owner(a), db=db)
    _, ch = await _rankings(db, a)
    by_key = {w["key"]: w for w in wallets["wallets"]}
    # 3 Uber sales today, 1 Mr D, 1 POS (voided and cancelled excluded by both)
    assert (by_key["UBER_EATS"]["order_count"], by_key["MR_D"]["order_count"], by_key["GENERAL"]["order_count"]) == (3, 1, 1)
    assert ch["UBER_EATS"]["total_item_gross_cents"] == 42000   # R440 order totals less the R20 delivery fee


async def test_invalid_rank_by_and_range(db):
    b = await _business(db)
    try:
        from backend.app.analytics.product_rankings import best_sellers
        from backend.app.analytics.sales_wallets import resolve_range
        with pytest.raises(AppError) as exc:
            await best_sellers(db, b, resolve_range("today", None, None, TZ), rank_by="name")
        assert exc.value.status_code == 422
        with pytest.raises(AppError) as exc:
            await get_best_sellers(range_="custom", start=date(2026, 11, 5), end=date(2026, 11, 1), rank_by="quantity",
                                   user=_owner(b), db=db)
        assert exc.value.code == "INVALID_DATE_RANGE"
    finally:
        await _cleanup(db, b)
