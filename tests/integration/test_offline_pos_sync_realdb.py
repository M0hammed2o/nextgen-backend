"""
Real-Postgres tests for accurate offline POS sync (Phase 3b, migration 0067).

Route functions are called directly with real sessions (same pattern as
test_souk_security_review_realdb.py). Covers: capture window, cashier
validation and cross-tenant ids, till-session attribution at the capture
instant, stock taken negative (accepted + flagged), server-authoritative price,
the trading gate evaluated on the captured date, duplicate replays (sequential
and concurrent), and the manager review list.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update

from backend.app.analytics.revenue import sale_timestamp_expr
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    TillCloseRequest,
    TillOpenRequest,
    close_till,
    create_pos_order,
    list_flagged_offline_orders,
    open_till,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import require_owner_or_manager
from backend.app.events.trading import (
    OFFLINE_CAPTURE_MAX_AGE,
    OFFLINE_CAPTURE_MAX_FUTURE_SKEW,
    assert_trading_allowed,
)
from backend.app.inventory.ledger import stock_on_hand
from shared.models.inventory import StockMovement
from shared.models.order import Order
from shared.models.pos_payment import POSPaymentAllocation
from shared.models.till import TillSession
from shared.models.user import BusinessUser
from tests.integration._db import get_session_factory
from tests.integration.test_product_variants_realdb import _matrix, _shop  # noqa: F401
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    DAYS,
    SAST,
    TODAY,
    _event,
    _pay,
    _plain_business,
    _row,
    _user,
    _vendors,
    bag,
)

pytestmark = pytest.mark.asyncio

PRICE = 5000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(**kw) -> datetime:
    return _now() - timedelta(**kw)


def _req(item_id, captured, key=None, qty=1, variant_id=None, **kw) -> POSOrderCreateRequest:
    return POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=item_id, variant_id=variant_id, quantity=qty)],
        payment_method="CASH", cash_tendered_cents=10_000_000,
        offline_captured_at=captured, idempotency_key=key or f"off-{uuid.uuid4().hex}", **kw,
    )


async def _item(db, owner, price=PRICE, name="Linen Shirt"):
    cat = await create_category(body=CategoryCreate(name="Cat"), user=owner, db=db)
    return await create_item(body=ItemCreate(category_id=cat.id, name=name, price_cents=price), user=owner, db=db)


async def _biz_with_item(db, bag, **kw):
    biz = await _plain_business(db, bag, pos_enabled=True, **kw)
    owner = _user(biz.id)
    return biz, owner, await _item(db, owner)


async def _fetch(db, order_id) -> Order:
    stmt = select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def _err(coro) -> tuple[int, str]:
    with pytest.raises((AppError, NotFoundError)) as exc:
        await coro
    return exc.value.status_code, exc.value.code


async def _user_row(db, biz_id, role="STAFF", active=True, name="Cashier") -> BusinessUser:
    u = BusinessUser(business_id=biz_id, role=role, staff_name=name, pin_hash="x", is_active=active)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _set_till(db, session_id, opened_at=None, closed_at=None):
    values = {}
    if opened_at is not None:
        values["opened_at"] = opened_at
    if closed_at is not None:
        values["closed_at"] = closed_at
    await db.execute(update(TillSession).where(TillSession.id == session_id).values(**values))
    await db.commit()


async def _backdate_open_till(db, biz_id, hours=2):
    """_shop() opens the till 'now'; move its opened_at back so recent captures fall inside it."""
    sid = (await db.execute(select(TillSession.id).where(
        TillSession.business_id == biz_id, TillSession.status == "OPEN"))).scalar_one()
    await _set_till(db, sid, opened_at=_ago(hours=hours))
    return sid


# -- request validation --------------------------------------------------------

async def test_request_validation_rules():
    item = uuid.uuid4()
    base = dict(items=[POSOrderItemRequest(menu_item_id=item, quantity=1)], payment_method="CASH",
                cash_tendered_cents=100)
    with pytest.raises(ValidationError):  # offline-only fields need a capture time
        POSOrderCreateRequest(**base, captured_by_user_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        POSOrderCreateRequest(**base, offline_client_total_cents=100)
    with pytest.raises(ValidationError):
        POSOrderCreateRequest(**base, is_offline_capture=True)
    with pytest.raises(ValidationError):  # contradiction
        POSOrderCreateRequest(**base, is_offline_capture=False, offline_captured_at=_now(), idempotency_key="k")
    with pytest.raises(ValidationError):  # an offline capture must carry an idempotency key
        POSOrderCreateRequest(**base, offline_captured_at=_now())
    with pytest.raises(ValidationError):
        POSOrderCreateRequest(**base, offline_captured_at=_now(), idempotency_key="k", offline_client_total_cents=-1)
    ok = POSOrderCreateRequest(**base, offline_captured_at=_now(), idempotency_key="k")
    assert ok.is_offline_capture is True
    assert POSOrderCreateRequest(**base).is_offline_capture is None  # online request unchanged


async def test_client_cannot_send_a_price_or_business_id():
    fields = POSOrderCreateRequest.model_fields
    assert not any("price" in f for f in fields if f != "offline_client_total_cents")
    assert "business_id" not in fields and "till_session_id" not in fields


async def test_flagged_list_is_manager_only():
    dep = inspect.signature(list_flagged_offline_orders).parameters["user"].default.dependency
    assert dep is require_owner_or_manager


# -- capture window ------------------------------------------------------------

async def test_capture_window_limits_are_module_constants():
    assert OFFLINE_CAPTURE_MAX_AGE == timedelta(hours=72)
    assert OFFLINE_CAPTURE_MAX_FUTURE_SKEW == timedelta(minutes=5)


async def test_capture_too_old_in_future_and_naive_are_422_and_leave_nothing_behind(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    assert await _err(create_pos_order(_req(item.id, _ago(hours=73)), owner, db)) == (422, "OFFLINE_CAPTURE_TOO_OLD")
    assert await _err(create_pos_order(_req(item.id, _now() + timedelta(minutes=10)), owner, db)) == (
        422, "OFFLINE_CAPTURE_IN_FUTURE")
    naive = datetime.now()  # no tzinfo
    assert await _err(create_pos_order(_req(item.id, naive), owner, db)) == (422, "OFFLINE_CAPTURE_NO_TIMEZONE")
    assert (await db.execute(select(func.count(Order.id)).where(Order.business_id == biz.id))).scalar_one() == 0
    seq = (await db.execute(text("select order_number_sequence from businesses where id = :i"), {"i": biz.id})).scalar_one()
    assert seq == 0  # no order number was burned


async def test_capture_just_inside_the_window_is_accepted(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    old = await create_pos_order(_req(item.id, _ago(hours=71, minutes=55)), owner, db)
    soon = await create_pos_order(_req(item.id, _now() + timedelta(minutes=2)), owner, db)
    assert old.is_offline_capture and soon.is_offline_capture


# -- cashier (captured_by_user_id) ---------------------------------------------

async def test_captured_by_defaults_to_the_caller_and_records_the_syncer(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    out = await create_pos_order(_req(item.id, _ago(minutes=20)), owner, db)
    assert out.created_by_user_id == owner.user_id and out.offline_synced_by_user_id == owner.user_id
    assert out.offline_synced_at is not None and out.is_offline_capture


async def test_captured_by_a_colleague_is_attributed_to_them_and_synced_by_the_caller(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    cashier = await _user_row(db, biz.id)
    out = await create_pos_order(_req(item.id, _ago(minutes=20), captured_by_user_id=cashier.id), owner, db)
    assert out.created_by_user_id == cashier.id
    assert out.offline_synced_by_user_id == owner.user_id


async def test_captured_by_must_be_an_active_user_of_the_callers_business(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    other_biz, _, _ = await _biz_with_item(db, bag)
    foreign = await _user_row(db, other_biz.id)
    inactive = await _user_row(db, biz.id, active=False)
    for who in (foreign.id, inactive.id, uuid.uuid4()):
        assert await _err(create_pos_order(_req(item.id, _ago(minutes=5), captured_by_user_id=who), owner, db)) == (
            404, "USER_NOT_FOUND")
    assert (await db.execute(select(func.count(Order.id)).where(Order.business_id == biz.id))).scalar_one() == 0


async def test_staff_discount_limit_applies_to_the_cashier_not_the_syncing_manager(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    await db.execute(text("update businesses set max_staff_discount_cents = 100 where id = :i"), {"i": biz.id})
    await db.commit()
    cashier = await _user_row(db, biz.id, role="STAFF")
    code = await _err(create_pos_order(
        _req(item.id, _ago(minutes=5), captured_by_user_id=cashier.id, discount_cents=1000), owner, db))
    assert code == (403, "DISCOUNT_LIMIT_EXCEEDED")
    ok = await create_pos_order(_req(item.id, _ago(minutes=5), discount_cents=1000), owner, db)  # owner as cashier
    assert ok.discount_cents == 1000


# -- till session attribution --------------------------------------------------

async def _two_sessions(db, owner):
    """T1 open -5h..closed -3h; gap; T2 open from -1h."""
    t1 = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    await _set_till(db, t1.id, opened_at=_ago(hours=5))
    await close_till(t1.id, TillCloseRequest(counted_cash_cents=0), owner, db)
    await _set_till(db, t1.id, closed_at=_ago(hours=3))
    t2 = await open_till(body=TillOpenRequest(opening_float_cents=1000), user=owner, db=db)
    await _set_till(db, t2.id, opened_at=_ago(hours=1))
    return t1, t2


async def test_sale_is_attributed_to_the_till_open_at_capture_time_not_at_sync_time(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    t1, t2 = await _two_sessions(db, owner)

    in_t1 = await create_pos_order(_req(item.id, _ago(hours=4)), owner, db)
    in_gap = await create_pos_order(_req(item.id, _ago(hours=2)), owner, db)
    in_t2 = await create_pos_order(_req(item.id, _ago(minutes=30)), owner, db)

    assert in_t1.till_session_id == t1.id            # NOT the session open now (T2)
    assert in_t1.offline_late_to_closed_till and in_t1.offline_review_required and not in_t1.offline_unattributed
    assert in_gap.till_session_id is None
    assert in_gap.offline_unattributed and in_gap.offline_review_required
    assert in_t2.till_session_id == t2.id
    assert not (in_t2.offline_unattributed or in_t2.offline_late_to_closed_till or in_t2.offline_review_required)
    # payment allocations follow the order's session, so cash-up totals do too
    allocs = {a.order_id: a.till_session_id for a in (await db.execute(
        select(POSPaymentAllocation).where(POSPaymentAllocation.business_id == biz.id))).scalars()}
    assert allocs == {in_t1.id: t1.id, in_gap.id: None, in_t2.id: t2.id}


async def test_cash_up_totals_follow_the_attributed_session_and_show_unattributed_sales(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    t1, t2 = await _two_sessions(db, owner)
    await create_pos_order(_req(item.id, _ago(hours=4)), owner, db)      # T1
    await create_pos_order(_req(item.id, _ago(hours=2)), owner, db)      # gap
    await create_pos_order(_req(item.id, _ago(minutes=30)), owner, db)   # T2
    await create_pos_order(  # an ordinary online sale in T2
        POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                              payment_method="CASH", cash_tendered_cents=PRICE), owner, db)

    closed = await close_till(t2.id, TillCloseRequest(counted_cash_cents=1000 + 2 * PRICE), owner, db)
    assert closed.cash_sales_cents == 2 * PRICE                       # only T2's two sales
    assert closed.expected_cash_cents == 1000 + 2 * PRICE and closed.variance_cents == 0
    assert closed.offline_unattributed_count == 1                      # the gap sale is shown, not hidden
    assert closed.offline_unattributed_cash_cents == PRICE


async def test_no_open_till_at_sync_is_fine_offline_but_still_refused_online(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)  # no till session at all
    out = await create_pos_order(_req(item.id, _ago(minutes=10)), owner, db)
    assert out.till_session_id is None and out.offline_unattributed and out.offline_review_required
    online = POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                                   payment_method="CASH", cash_tendered_cents=PRICE)
    assert await _err(create_pos_order(online, owner, db)) == (409, "TILL_NOT_OPEN")


async def test_another_businesss_till_session_is_never_used(db, bag):
    biz_a, owner_a, item_a = await _biz_with_item(db, bag)   # has no till
    biz_b, owner_b, item_b = await _biz_with_item(db, bag)
    t_b = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner_b, db=db)
    await _set_till(db, t_b.id, opened_at=_ago(hours=2))
    out = await create_pos_order(_req(item_a.id, _ago(minutes=10)), owner_a, db)
    assert out.till_session_id is None and out.offline_unattributed
    n = (await db.execute(select(func.count(POSPaymentAllocation.id)).where(
        POSPaymentAllocation.till_session_id == t_b.id))).scalar_one()
    assert n == 0


async def test_sale_before_the_till_opened_is_unattributed_not_pushed_into_it(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    t = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    await _set_till(db, t.id, opened_at=_ago(minutes=30))
    out = await create_pos_order(_req(item.id, _ago(minutes=45)), owner, db)
    assert out.till_session_id is None and out.offline_unattributed


# -- stock as it happened ------------------------------------------------------

async def test_offline_sale_is_accepted_even_when_it_takes_stock_negative_and_is_flagged(db, bag):
    biz, owner, item = await _shop(db, bag)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=1)).values())
    captured = _ago(minutes=45)
    out = await create_pos_order(_req(item.id, captured, qty=3, variant_id=variant.id), owner, db)
    assert out.offline_stock_shortfall and out.offline_review_required
    on_hand = (await stock_on_hand(db, biz.id, ingredient_ids=[variant.ingredient_id]))[variant.ingredient_id]
    assert on_hand == (1 - 3) * 1000                          # the ledger tells the truth: -2 units
    mv = (await db.execute(select(StockMovement).where(StockMovement.source_id == out.id))).scalars().all()
    assert [m.quantity_milli for m in mv] == [-3000]
    assert abs((mv[0].occurred_at - captured).total_seconds()) < 1  # dated when it happened


async def test_online_sale_behaviour_is_unchanged_negative_stock_is_not_flagged(db, bag):
    biz, owner, item = await _shop(db, bag)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=1)).values())
    online = await create_pos_order(POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=item.id, variant_id=variant.id, quantity=3)],
        payment_method="CASH", cash_tendered_cents=10_000_000), owner, db)
    assert online.is_offline_capture is False and not online.offline_stock_shortfall
    assert not online.offline_review_required and online.offline_captured_at is None
    assert online.status == "COMPLETED" or online.status == "ACCEPTED"


async def test_offline_sale_within_stock_is_not_flagged(db, bag):
    biz, owner, item = await _shop(db, bag)
    till_id = await _backdate_open_till(db, biz.id)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=5)).values())
    out = await create_pos_order(_req(item.id, _ago(minutes=5), qty=2, variant_id=variant.id), owner, db)
    assert not out.offline_stock_shortfall and not out.offline_review_required
    assert out.till_session_id == till_id


# -- price is server-authoritative ---------------------------------------------

async def test_client_total_is_never_used_a_difference_is_recorded_and_flagged(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    t = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    await _set_till(db, t.id, opened_at=_ago(hours=1))
    cheap = await create_pos_order(_req(item.id, _ago(minutes=5), offline_client_total_cents=100), owner, db)
    assert cheap.total_cents == PRICE and cheap.subtotal_cents == PRICE
    assert cheap.offline_client_total_cents == 100 and cheap.offline_price_variance_cents == 100 - PRICE
    assert cheap.offline_review_required
    dear = await create_pos_order(_req(item.id, _ago(minutes=5), offline_client_total_cents=PRICE + 700), owner, db)
    assert dear.total_cents == PRICE and dear.offline_price_variance_cents == 700 and dear.offline_review_required
    same = await create_pos_order(_req(item.id, _ago(minutes=5), offline_client_total_cents=PRICE), owner, db)
    assert same.offline_price_variance_cents is None and not same.offline_review_required
    none = await create_pos_order(_req(item.id, _ago(minutes=5)), owner, db)
    assert none.offline_client_total_cents is None and none.offline_price_variance_cents is None


async def test_offline_sale_is_recorded_at_its_capture_time(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    captured = _ago(hours=5)
    out = await create_pos_order(_req(item.id, captured), owner, db)
    row = await _fetch(db, out.id)
    assert row.status == "COMPLETED" and row.payment_status == "PAID"
    assert row.paid_at == captured == row.confirmed_at == row.completed_at
    assert row.created_at > captured + timedelta(hours=4)
    ts = (await db.execute(select(sale_timestamp_expr()).where(Order.id == out.id))).scalar_one()
    assert ts == captured  # analytics bucket the sale when it happened


# -- trading gate on the captured date -----------------------------------------

def _sast(day, hour=12) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=SAST).astimezone(timezone.utc)


async def _gate(db, biz_id, now, captured):
    try:
        await assert_trading_allowed(db, biz_id, now, captured_at=captured)
        return "TRADE"
    except AppError as exc:
        return (exc.status_code, exc.code, (exc.details or {}).get("reason"))


async def test_gate_capture_on_an_unpaid_day_is_refused_even_when_synced_on_a_paid_day(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[1], ends=DAYS[1])])
    await _pay(db, ev, v)                                  # paid for DAYS[1] only
    sync_time = _sast(DAYS[1], 9)                          # synced on the paid day
    assert await _gate(db, v["business_id"], sync_time, None) == "TRADE"  # online "today" is paid
    refused = await _gate(db, v["business_id"], sync_time, _sast(DAYS[0], 20))  # rung up the day before
    assert refused == (403, "EVENT_TRADING_NOT_ALLOWED", "NO_PAID_COVER_TODAY")


async def test_gate_capture_on_a_paid_day_synced_after_the_event_ended_is_accepted(db, bag):
    ev = await _event(db, bag, days=DAYS[:3])
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[2], ends=DAYS[2])])
    await _pay(db, ev, v)
    morning_after = _sast(DAYS[3], 9)                      # the event is over
    online = await _gate(db, v["business_id"], morning_after, None)
    assert online == (403, "EVENT_TRADING_NOT_ALLOWED", "EVENT_ENDED")           # online behaviour unchanged
    assert await _gate(db, v["business_id"], morning_after, _sast(DAYS[2], 21)) == "TRADE"


async def test_gate_capture_on_a_paid_day_but_older_than_72h_is_refused(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0])])
    await _pay(db, ev, v)
    captured = _sast(DAYS[0], 10)
    assert await _gate(db, v["business_id"], captured + timedelta(hours=71), captured) == "TRADE"
    assert await _gate(db, v["business_id"], captured + timedelta(hours=73), captured) == (
        422, "OFFLINE_CAPTURE_TOO_OLD", None)
    assert await _gate(db, v["business_id"], captured - timedelta(minutes=10), captured) == (
        422, "OFFLINE_CAPTURE_IN_FUTURE", None)


async def test_gate_capture_never_affects_a_non_event_business(db, bag):
    biz = await _plain_business(db, bag, pos_enabled=True)
    await assert_trading_allowed(db, biz.id, captured_at=_ago(hours=2))
    assert await _gate(db, biz.id, _now(), _ago(hours=80)) == (422, "OFFLINE_CAPTURE_TOO_OLD", None)


async def test_event_vendor_route_judges_the_captured_day_not_the_sync_day(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0])])
    await _pay(db, ev, v)                                  # paid today only
    owner = _user(v["business_id"])
    item = await _item(db, owner)
    # rung up on the unpaid day before, synced today (a paid day): refused
    yesterday = _sast(TODAY - timedelta(days=1), 20)
    code = await _err(create_pos_order(_req(item.id, yesterday), owner, db))
    assert code == (403, "EVENT_TRADING_NOT_ALLOWED")
    # rung up earlier today (a paid day): accepted
    out = await create_pos_order(_req(item.id, min(_sast(TODAY, 0), _now()) + timedelta(minutes=1)), owner, db)
    assert out.is_offline_capture and out.payment_status == "PAID"


# -- duplicates ----------------------------------------------------------------

async def _count(db, biz_id, key):
    return (await db.execute(select(func.count(Order.id)).where(
        Order.business_id == biz_id, Order.idempotency_key == key))).scalar_one()


async def test_replaying_the_same_key_returns_the_original_and_deducts_stock_once(db, bag):
    biz, owner, item = await _shop(db, bag)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=5)).values())
    key = f"dup-{uuid.uuid4().hex}"
    first = await create_pos_order(_req(item.id, _ago(minutes=30), key=key, variant_id=variant.id), owner, db)
    again = await create_pos_order(_req(item.id, _ago(minutes=30), key=key, variant_id=variant.id), owner, db)
    # a retry after the 72h window (stale capture time) still replays instead of failing
    stale = await create_pos_order(_req(item.id, _ago(hours=100), key=key, variant_id=variant.id), owner, db)
    assert first.id == again.id == stale.id
    assert first.order_number == again.order_number
    assert await _count(db, biz.id, key) == 1
    assert (await stock_on_hand(db, biz.id, ingredient_ids=[variant.ingredient_id]))[variant.ingredient_id] == 4000
    n_mv = (await db.execute(select(func.count(StockMovement.id)).where(StockMovement.source_id == first.id))).scalar_one()
    assert n_mv == 1


async def test_two_simultaneous_syncs_of_the_same_key_make_one_order_and_one_deduction(db, bag):
    biz, owner, item = await _shop(db, bag)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=50)).values())
    factory = get_session_factory()

    async def sync(key, captured):
        async with factory() as s:
            return await create_pos_order(_req(item.id, captured, key=key, variant_id=variant.id), owner, s)

    keys = [f"race-{uuid.uuid4().hex}" for _ in range(6)]
    for key in keys:
        captured = _ago(minutes=3)
        a, b = await asyncio.gather(sync(key, captured), sync(key, captured))
        assert a.id == b.id
        assert await _count(db, biz.id, key) == 1
    assert (await db.execute(select(func.count(Order.id)).where(Order.business_id == biz.id))).scalar_one() == len(keys)
    assert (await stock_on_hand(db, biz.id, ingredient_ids=[variant.ingredient_id]))[variant.ingredient_id] == (
        (50 - len(keys)) * 1000)
    n_mv = (await db.execute(select(func.count(StockMovement.id)).where(
        StockMovement.business_id == biz.id, StockMovement.source_type == "ORDER"))).scalar_one()
    assert n_mv == len(keys)
    allocs = (await db.execute(select(func.count(POSPaymentAllocation.id)).where(
        POSPaymentAllocation.business_id == biz.id))).scalar_one()
    assert allocs == len(keys)


async def test_keys_are_scoped_per_business(db, bag):
    biz_a, owner_a, item_a = await _biz_with_item(db, bag)
    biz_b, owner_b, item_b = await _biz_with_item(db, bag)
    key = f"same-{uuid.uuid4().hex}"
    a = await create_pos_order(_req(item_a.id, _ago(minutes=5), key=key), owner_a, db)
    b = await create_pos_order(_req(item_b.id, _ago(minutes=5), key=key), owner_b, db)
    assert a.id != b.id  # another business's identical key is not a replay


# -- reporting -----------------------------------------------------------------

async def test_flagged_list_filters_by_reason_and_is_tenant_scoped(db, bag):
    biz, owner, item = await _shop(db, bag)
    await _backdate_open_till(db, biz.id)
    [variant] = list((await _matrix(db, owner, item, sizes=["S"], colours=["Black"], opening_stock=1)).values())
    clean = await create_pos_order(_req(item.id, _ago(minutes=1), qty=1, variant_id=variant.id), owner, db)
    c_short = _ago(minutes=2)
    short = await create_pos_order(_req(item.id, c_short, qty=4, variant_id=variant.id), owner, db)
    var = await create_pos_order(_req(item.id, _ago(minutes=3), variant_id=variant.id,
                                      offline_client_total_cents=1), owner, db)
    unattr = await create_pos_order(_req(item.id, _ago(hours=10), variant_id=variant.id), owner, db)
    other, other_owner, other_item = await _biz_with_item(db, bag)
    foreign = await create_pos_order(_req(other_item.id, _ago(minutes=1)), other_owner, db)  # flagged: no till
    assert foreign.offline_review_required

    async def ids(**kw):
        args = dict(reason=None, since=None, limit=50)
        args.update(kw)
        return {o.id for o in await list_flagged_offline_orders(user=owner, db=db, **args)}

    assert await ids() == {short.id, var.id, unattr.id}
    assert clean.id not in await ids() and foreign.id not in await ids()
    assert await ids(reason="shortfall") >= {short.id} and short.id in await ids(reason="shortfall")
    assert await ids(reason="variance") == {var.id}
    assert await ids(reason="unattributed") == {unattr.id}
    assert await ids(since=c_short - timedelta(seconds=30)) == {short.id}  # var was captured a minute earlier
    assert len(await ids(limit=1)) == 1
    row = (await list_flagged_offline_orders(user=owner, db=db, reason="variance", since=None, limit=5))[0]
    assert row.offline_price_variance_cents == 1 - row.total_cents and row.offline_client_total_cents == 1
