"""
Real-Postgres tests for souk Phase 1: access levels, the server-side trading
gate, billing safeguards (single-use payment reference, event-row locking) and
the new admin / business-plane endpoints. Route functions are called directly
with real sessions, the same pattern as test_event_admin_realdb.py.

The calendar is relative to today (Africa/Johannesburg) because the routes run
the access reconcile on the real clock.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from admin_api.app.api.v1.routes_events import (
    BulkVendorRequest,
    CalendarUpdate,
    EventCreate,
    EventDayIn,
    EventUpdate,
    MarkPaid,
    SubscriptionCreate,
    VendorRow,
    VendorSuspend,
    VendorUpdate,
    _load_event,
    bulk_onboard_vendors,
    create_event,
    create_subscription,
    event_revenue,
    list_vendors,
    mark_subscription_paid,
    reinstate_vendor,
    replace_calendar,
    suspend_vendor,
    update_event,
    update_vendor,
)
from backend.app.api.v1.routes_business import get_event_access
from backend.app.api.v1.routes_orders import OrderCreate, OrderItemCreate, PaymentUpdateRequest, create_order
from backend.app.api.v1.routes_orders import update_payment_status
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSPaymentIntentCreateRequest,
    create_pos_order,
    create_pos_payment_intent,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.events.service import EVENT_ADMIN_SUSPENSION_REASON, EVENT_SUSPENSION_REASON, reconcile_event_vendor_access
from backend.app.events.trading import assert_trading_allowed
from shared.models.business import Business
from shared.models.event import Event, EventVendor, EventVendorSubscription
from shared.models.order import Order
from shared.models.user import BusinessUser
from tests.integration._db import get_session_factory

pytestmark = pytest.mark.asyncio

SAST = ZoneInfo("Africa/Johannesburg")
TODAY = datetime.now(SAST).date()
DAYS = [TODAY + timedelta(days=i) for i in range(5)]  # today .. today+4
ADMIN = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


def _user(business_id, role="STAFF") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


@pytest.fixture
async def bag(db):
    bag = {"events": [], "emails": [], "orders": [], "extra_biz": []}
    yield bag
    await db.rollback()
    if bag["orders"]:
        await db.execute(delete(Order).where(Order.id.in_(bag["orders"])))
    if bag["emails"]:
        biz_ids = (await db.execute(
            select(BusinessUser.business_id).where(BusinessUser.email.in_(bag["emails"])))).scalars().all()
        if biz_ids:
            await db.execute(delete(Business).where(Business.id.in_(biz_ids)))
    if bag["extra_biz"]:
        await db.execute(delete(Business).where(Business.id.in_(bag["extra_biz"])))
    if bag["events"]:
        await db.execute(delete(Event).where(Event.id.in_(bag["events"])))
    await db.commit()


async def _event(db, bag, days=DAYS, **kw) -> dict:
    ev = await create_event(EventCreate(name=f"P1 Souk {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=d) for d in days], **kw), ADMIN, db)
    bag["events"].append(ev["id"])
    return ev


def _row(bag, category="FOOD", stall=None, starts=None, ends=None, **kw) -> VendorRow:
    email = f"p1-{uuid.uuid4().hex[:8]}@example.com"
    bag["emails"].append(email)
    base = dict(category=category, stall_code=stall, business_name=f"P1 Stall {uuid.uuid4().hex[:5]}",
                owner_email=email, owner_full_name="Vendor Owner")
    if starts:
        base.update(starts_on=starts, ends_on=ends or starts)
    base.update(kw)
    return VendorRow(**base)


async def _vendors(db, ev, bag, rows) -> list[dict]:
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    return res["vendors"]


async def _sub_id(db, ev_id, vendor_id):
    items = (await list_vendors(ev_id, ADMIN, db))["items"]
    return next(v for v in items if v["vendor_id"] == vendor_id)["subscriptions"][0]["id"]


async def _pay(db, ev, vendor, ref=None):
    sub_id = await _sub_id(db, ev["id"], vendor["vendor_id"])
    return await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference=ref or f"ref-{uuid.uuid4().hex}"),
                                        ADMIN, db)


async def _refused(coro) -> str:
    with pytest.raises(AppError) as exc:
        await coro
    return exc.value.code


# ── Event editing / prep_days ─────────────────────────────────────────────────

async def test_prep_days_defaults_to_14_and_is_editable(db, bag):
    ev = await _event(db, bag)
    assert ev["prep_days"] == 14
    out = await update_event(ev["id"], EventUpdate(name="Renamed Souk", venue="Hall 2", prep_days=30), ADMIN, db)
    assert (out["name"], out["venue"], out["prep_days"]) == ("Renamed Souk", "Hall 2", 30)
    assert out["food_day_rate_cents"] == 10_000
    assert "notes" in ev and "notes" in out and out["notes"] is None
    noted = await update_event(ev["id"], EventUpdate(notes="Bring ID"), ADMIN, db)
    assert noted["notes"] == "Bring ID" and noted["prep_days"] == 30  # PATCH response has GET shape


async def test_prep_days_range_is_enforced_in_api_and_database(db, bag):
    with pytest.raises(ValidationError):
        EventUpdate(prep_days=91)
    with pytest.raises(ValidationError):
        EventUpdate(prep_days=-1)
    with pytest.raises(ValidationError):
        EventCreate(name="Bad Prep", prep_days=500)
    ev = await _event(db, bag)
    row = (await db.execute(select(Event).where(Event.id == ev["id"]))).scalar_one()
    row.prep_days = 91
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_rates_are_not_editable_and_empty_patch_is_refused(db, bag):
    with pytest.raises(ValidationError):
        EventUpdate(food_day_rate_cents=1)
    ev = await _event(db, bag)
    assert await _refused(update_event(ev["id"], EventUpdate(), ADMIN, db)) == "NO_VALID_FIELDS"


async def test_changing_prep_days_moves_the_window_and_reconciles(db, bag):
    start = TODAY + timedelta(days=20)
    ev = await _event(db, bag, days=[start, start + timedelta(days=1)])
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    biz_id = v["business_id"]
    await reconcile_event_vendor_access(db); await db.commit()
    biz = (await db.execute(select(Business).where(Business.id == biz_id))).scalar_one()
    assert biz.is_active is False and biz.suspended_reason == EVENT_SUSPENSION_REASON  # 20 days out, 14-day window
    await update_event(ev["id"], EventUpdate(prep_days=25), ADMIN, db)
    await db.refresh(biz)
    assert biz.is_active is True and biz.suspended_reason is None  # window now open: CONFIGURE


# ── Vendor list ───────────────────────────────────────────────────────────────

async def test_vendor_list_shape_filters_search_and_paging(db, bag):
    ev = await _event(db, bag)
    vs = await _vendors(db, ev, bag, [
        _row(bag, "FOOD", stall="AA1", starts=DAYS[0], ends=DAYS[1]),
        _row(bag, "CLOTHING", stall="BB2"),
        _row(bag, "FOOD", stall="AA3"),
    ])
    await _pay(db, ev, vs[0])
    page = await list_vendors(ev["id"], ADMIN, db)
    assert set(page) == {"items", "total"} and page["total"] == 3
    by_id = {v["vendor_id"]: v for v in page["items"]}
    assert by_id[vs[0]["vendor_id"]]["access_level"] == "TRADE"
    assert by_id[vs[1]["vendor_id"]]["access_level"] == "CONFIGURE"
    assert all({"business_active", "suspended_reason", "access_level"} <= set(v) for v in page["items"])
    assert (await list_vendors(ev["id"], ADMIN, db, category="CLOTHING"))["total"] == 1
    assert (await list_vendors(ev["id"], ADMIN, db, status="ACTIVE"))["total"] == 1
    assert (await list_vendors(ev["id"], ADMIN, db, q="bb2"))["items"][0]["stall_code"] == "BB2"
    code = vs[2]["business_code"]
    assert [i["business_code"] for i in (await list_vendors(ev["id"], ADMIN, db, q=code.lower()))["items"]] == [code]
    assert (await list_vendors(ev["id"], ADMIN, db, q="100%_"))["total"] == 0  # LIKE wildcards are escaped
    first = await list_vendors(ev["id"], ADMIN, db, limit=2, offset=0)
    rest = await list_vendors(ev["id"], ADMIN, db, limit=2, offset=2)
    assert (len(first["items"]), len(rest["items"]), first["total"], rest["total"]) == (2, 1, 3, 3)
    assert {i["vendor_id"] for i in first["items"]} | {i["vendor_id"] for i in rest["items"]} == set(by_id)


# ── Stall allocation ──────────────────────────────────────────────────────────

async def test_stall_allocation_unique_and_clearable(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, stall="S1"), _row(bag)])
    assert (await update_vendor(ev["id"], b["vendor_id"], VendorUpdate(stall_code="S2"), ADMIN, db))["stall_code"] == "S2"
    assert await _refused(update_vendor(ev["id"], b["vendor_id"], VendorUpdate(stall_code="S1"), ADMIN, db)) == "STALL_TAKEN"
    assert (await update_vendor(ev["id"], a["vendor_id"], VendorUpdate(stall_code=None), ADMIN, db))["stall_code"] is None
    assert (await update_vendor(ev["id"], b["vendor_id"], VendorUpdate(stall_code="S1"), ADMIN, db))["stall_code"] == "S1"
    assert await _refused(update_vendor(ev["id"], a["vendor_id"], VendorUpdate(), ADMIN, db)) == "NO_VALID_FIELDS"


# ── Admin suspend / reinstate ─────────────────────────────────────────────────

async def test_admin_suspension_is_never_lifted_by_reconcile_only_by_reinstate(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[4])])
    await _pay(db, ev, v)
    out = await suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="complaint"), ADMIN, db)
    assert out == {"vendor_id": v["vendor_id"], "business_active": False, "suspended_reason": EVENT_ADMIN_SUSPENSION_REASON}
    res = await reconcile_event_vendor_access(db); await db.commit()
    assert v["business_id"] not in res.businesses_reactivated
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_ADMIN_SUSPENSION_REASON)
    assert await _refused(suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="x"), ADMIN, db)) == "ALREADY_SUSPENDED"
    back = await reinstate_vendor(ev["id"], v["vendor_id"], ADMIN, db)
    assert back["business_active"] is True and back["suspended_reason"] is None
    assert await _refused(reinstate_vendor(ev["id"], v["vendor_id"], ADMIN, db)) == "NOT_ADMIN_SUSPENDED"


async def test_reinstate_refuses_another_suspension_reason(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    biz.is_active, biz.suspended_reason = False, "Fraud review"
    await db.commit()
    assert await _refused(reinstate_vendor(ev["id"], v["vendor_id"], ADMIN, db)) == "NOT_ADMIN_SUSPENDED"
    await db.refresh(biz)
    assert (biz.is_active, biz.suspended_reason) == (False, "Fraud review")


async def test_vendor_routes_are_scoped_to_their_own_event(db, bag):
    ev1, ev2 = await _event(db, bag), await _event(db, bag)
    [v] = await _vendors(db, ev1, bag, [_row(bag)])
    with pytest.raises(AppError) as exc:
        await suspend_vendor(ev2["id"], v["vendor_id"], VendorSuspend(reason="x"), ADMIN, db)
    assert exc.value.status_code == 404
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    assert biz.is_active is not None and biz.suspended_reason != EVENT_ADMIN_SUSPENSION_REASON


# ── Revenue ───────────────────────────────────────────────────────────────────

async def test_revenue_by_category_and_day(db, bag):
    ev = await _event(db, bag)
    f, c, u = await _vendors(db, ev, bag, [
        _row(bag, "FOOD", starts=DAYS[0], ends=DAYS[2]),       # 3 days x 10000 = 30000, paid
        _row(bag, "CLOTHING", starts=DAYS[1], ends=DAYS[2]),   # 2 days x 5000 = 10000, paid
        _row(bag, "FOOD", starts=DAYS[0], ends=DAYS[0]),       # 10000, left unpaid
    ])
    await _pay(db, ev, f)
    await _pay(db, ev, c)
    r = await event_revenue(ev["id"], ADMIN, db)
    assert (r["paid_cents"], r["outstanding_cents"]) == (40_000, 10_000)
    assert r["by_category"]["FOOD"] == {"paid_cents": 30_000, "outstanding_cents": 10_000, "vendors": 2}
    assert r["by_category"]["CLOTHING"] == {"paid_cents": 10_000, "outstanding_cents": 0, "vendors": 1}
    by_day = {d["day"]: d for d in r["by_day"]}
    assert len(r["by_day"]) == 5
    assert (by_day[DAYS[0]]["vendors_covered"], by_day[DAYS[0]]["paid_cents_attributed"]) == (1, 10_000)
    assert (by_day[DAYS[1]]["vendors_covered"], by_day[DAYS[1]]["paid_cents_attributed"]) == (2, 15_000)
    assert (by_day[DAYS[3]]["vendors_covered"], by_day[DAYS[3]]["paid_cents_attributed"]) == (0, 0)
    assert sum(d["paid_cents_attributed"] for d in r["by_day"]) == r["paid_cents"]


# ── Billing safeguards ────────────────────────────────────────────────────────

async def test_payment_reference_may_settle_only_one_subscription(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0]), _row(bag, starts=DAYS[0], ends=DAYS[0])])
    ref = f"shared-{uuid.uuid4().hex}"
    await _pay(db, ev, a, ref)
    assert await _refused(_pay(db, ev, b, ref)) == "PAYMENT_REFERENCE_USED"
    again = await _pay(db, ev, a, ref)  # same subscription, same reference: still idempotent
    assert again["already_paid"] is True
    sub_b = (await db.execute(select(EventVendorSubscription).where(
        EventVendorSubscription.business_id == b["business_id"]))).scalar_one()
    assert sub_b.status == "UNPAID"


async def test_database_refuses_a_reused_reference_on_paid_rows(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0]), _row(bag, starts=DAYS[0], ends=DAYS[0])])
    ref = f"idx-{uuid.uuid4().hex}"
    await _pay(db, ev, a, ref)
    sub_b = (await db.execute(select(EventVendorSubscription).where(
        EventVendorSubscription.business_id == b["business_id"]))).scalar_one()
    # mark-paid stores references normalised (strip + upper-case, review L6); the index sees that form
    sub_b.status, sub_b.paid_at, sub_b.payment_reference = "PAID", datetime.now(timezone.utc), ref.strip().upper()
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_same_reference_raced_across_two_events_settles_only_one(db, bag):
    ev1, ev2 = await _event(db, bag), await _event(db, bag)
    [a] = await _vendors(db, ev1, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0])])
    [b] = await _vendors(db, ev2, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0])])
    sub_a, sub_b = await _sub_id(db, ev1["id"], a["vendor_id"]), await _sub_id(db, ev2["id"], b["vendor_id"])
    ref = f"race-{uuid.uuid4().hex}"

    async def pay(ev_id, sub_id):
        async with get_session_factory()() as s:
            try:
                await mark_subscription_paid(ev_id, sub_id, MarkPaid(payment_reference=ref), ADMIN, s)
                return "ok"
            except AppError as exc:
                return exc.code
            finally:
                await s.rollback()

    results = sorted(await asyncio.gather(pay(ev1["id"], sub_a), pay(ev2["id"], sub_b)))
    assert results == ["PAYMENT_REFERENCE_USED", "ok"]


async def test_event_row_lock_serializes_calendar_replace_against_mark_paid(db, bag):
    """
    Deterministic two-connection proof. Session A holds the event row lock (as
    mark-paid does); session B's calendar replace must wait for it, then see the
    PAID subscription and refuse to remove the paid day.
    """
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[2])])
    sub_id = await _sub_id(db, ev["id"], v["vendor_id"])
    new_days = [EventDayIn(day=d) for d in DAYS if d != DAYS[1]]  # drops a day inside the quoted range

    async with get_session_factory()() as a:
        await _load_event(a, ev["id"], lock=True)  # A: lock held, nothing committed yet

        async def replace():
            async with get_session_factory()() as b:
                try:
                    await replace_calendar(ev["id"], CalendarUpdate(days=new_days), ADMIN, b)
                    return "ok"
                except AppError as exc:
                    return exc.code
                finally:
                    await b.rollback()

        task = asyncio.create_task(replace())
        await asyncio.sleep(1.0)
        assert not task.done(), "calendar replace did not wait for the event row lock"
        await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference=f"lock-{uuid.uuid4().hex}"), ADMIN, a)
        assert await asyncio.wait_for(task, 10) == "PAID_DAY_REMOVED"

    day_rows = (await list_vendors(ev["id"], ADMIN, db))["items"][0]["subscriptions"][0]
    assert day_rows["status"] == "PAID" and day_rows["billable_days"] == 3


async def test_mark_paid_after_calendar_replace_is_stale(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[2])])
    sub_id = await _sub_id(db, ev["id"], v["vendor_id"])
    await replace_calendar(ev["id"], CalendarUpdate(days=[EventDayIn(day=d) for d in DAYS if d != DAYS[1]]), ADMIN, db)
    assert await _refused(mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="r-stale"), ADMIN, db)) \
        == "QUOTE_STALE"


# ── Trading gate ──────────────────────────────────────────────────────────────

async def _gate(db, business_id, now=None):
    try:
        await assert_trading_allowed(db, business_id, now)
        return "TRADE"
    except AppError as exc:
        assert exc.status_code == 403 and exc.code == "EVENT_TRADING_NOT_ALLOWED"
        return exc.details["reason"]


async def test_gate_allows_only_a_paid_billable_day(db, bag):
    ev = await _event(db, bag)
    paid, unpaid = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1]), _row(bag, starts=DAYS[0], ends=DAYS[1])])
    await _pay(db, ev, paid)
    assert await _gate(db, paid["business_id"]) == "TRADE"
    assert await _gate(db, unpaid["business_id"]) == "NO_PAID_COVER_TODAY"
    # live, not cached: tomorrow-but-one the paid cover has lapsed
    later = datetime.combine(DAYS[3], datetime.min.time(), tzinfo=SAST).replace(hour=12).astimezone(timezone.utc)
    assert await _gate(db, paid["business_id"], later) == "NO_PAID_COVER_TODAY"
    # the day before the event: prep window
    before = datetime.combine(TODAY - timedelta(days=1), datetime.min.time(), tzinfo=SAST).replace(hour=12).astimezone(timezone.utc)
    assert await _gate(db, paid["business_id"], before) == "PREP_WINDOW"


async def test_gate_refuses_when_no_calendar_is_published_even_if_paid_in_theory(db, bag):
    ev = await _event(db, bag, days=[])
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    assert await _gate(db, v["business_id"]) == "CALENDAR_NOT_PUBLISHED"


async def test_gate_refuses_a_non_billable_day(db, bag):
    ev = await create_event(EventCreate(name=f"P1 Gap {uuid.uuid4().hex[:6]}", days=[
        EventDayIn(day=TODAY - timedelta(days=1)), EventDayIn(day=TODAY, is_billable=False),
        EventDayIn(day=TODAY + timedelta(days=1))]), ADMIN, db)
    bag["events"].append(ev["id"])
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    assert await _gate(db, v["business_id"]) == "NOT_AN_EVENT_DAY"


async def test_gate_refuses_cancelled_vendor_and_event_and_never_affects_other_plans(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    await _pay(db, ev, v)
    assert await _gate(db, v["business_id"]) == "TRADE"
    row = (await db.execute(select(Event).where(Event.id == ev["id"]))).scalar_one()
    row.status = "CANCELLED"
    await db.commit()
    assert await _gate(db, v["business_id"]) == "CANCELLED"
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    biz.plan = "GROWTH"  # an ordinary restaurant that once traded at a souk
    await db.commit()
    await assert_trading_allowed(db, v["business_id"])  # no exception


async def test_gate_takes_the_highest_level_across_events_and_returning_vendor(db, bag):
    ev1, ev2 = await _event(db, bag), await _event(db, bag)
    [v1] = await _vendors(db, ev1, bag, [_row(bag)])  # unpaid at event 1
    # the same business returns at event 2 and pays there
    res = await bulk_onboard_vendors(ev2["id"], BulkVendorRequest(vendors=[VendorRow(
        category="FOOD", existing_business_id=v1["business_id"], starts_on=DAYS[0], ends_on=DAYS[0])]), ADMIN, db)
    [v2] = res["vendors"]
    assert await _gate(db, v1["business_id"]) == "NO_PAID_COVER_TODAY"
    await _pay(db, ev2, v2)
    assert await _gate(db, v1["business_id"]) == "TRADE"


async def test_event_plan_business_with_no_event_cannot_trade(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    await db.execute(delete(EventVendor).where(EventVendor.id == v["vendor_id"]))
    await db.commit()
    assert await _gate(db, v["business_id"]) == "NO_EVENT"


async def test_every_sale_and_payment_endpoint_refuses_an_unpaid_event_vendor(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])  # unpaid today: CONFIGURE
    user = _user(v["business_id"], "OWNER")
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    biz.is_active = True
    await db.commit()

    pos_body = POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
                                     payment_method="CARD")
    assert await _refused(create_pos_order(pos_body, user, db)) == "EVENT_TRADING_NOT_ALLOWED"
    assert await _refused(create_pos_payment_intent(
        POSPaymentIntentCreateRequest(amount_cents=1000, customer_phone="0821234567"), user, db)) \
        == "EVENT_TRADING_NOT_ALLOWED"
    manual = OrderCreate(items=[OrderItemCreate(name="Ad hoc", qty=1, price_cents=1000)])
    assert await _refused(create_order(manual, user, db)) == "EVENT_TRADING_NOT_ALLOWED"

    order = Order(business_id=v["business_id"], order_number=f"BO-{uuid.uuid4().hex[:8].upper()}", status="NEW",
                  payment_status="UNPAID", source="MANUAL", total_cents=1000, currency="ZAR")
    db.add(order)
    await db.commit()
    bag["orders"].append(order.id)
    assert await _refused(update_payment_status(
        order.id, PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"), user, db)) \
        == "EVENT_TRADING_NOT_ALLOWED"
    await db.refresh(order)
    assert order.payment_status == "UNPAID"


async def test_non_event_business_is_not_refused_by_the_sale_endpoints(db, bag):
    from shared.utils import generate_business_code
    u = uuid.uuid4().hex[:8]
    biz = Business(name=f"Plain {u}", slug=f"plain-{u}", business_code=generate_business_code(), currency="ZAR",
                   timezone="Africa/Johannesburg")
    db.add(biz)
    await db.commit()
    bag["extra_biz"].append(biz.id)
    order = Order(business_id=biz.id, order_number=f"BO-{uuid.uuid4().hex[:8].upper()}", status="NEW",
                  payment_status="UNPAID", source="MANUAL", total_cents=1000, currency="ZAR")
    db.add(order)
    await db.commit()
    bag["orders"].append(order.id)
    out = await update_payment_status(order.id, PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"),
                                      _user(biz.id), db)
    assert out.payment_status == "PAID"


async def test_paid_event_vendor_passes_the_payment_gate(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    await _pay(db, ev, v)
    order = Order(business_id=v["business_id"], order_number=f"BO-{uuid.uuid4().hex[:8].upper()}", status="NEW",
                  payment_status="UNPAID", source="MANUAL", total_cents=1000, currency="ZAR")
    db.add(order)
    await db.commit()
    bag["orders"].append(order.id)
    out = await update_payment_status(order.id, PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"),
                                      _user(v["business_id"]), db)
    assert out.payment_status == "PAID"


# ── Business-plane read: GET /business/event-access ───────────────────────────

async def test_event_access_endpoint_reports_own_level_events_and_subscriptions(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, stall="A1", starts=DAYS[0], ends=DAYS[1]), _row(bag, stall="B1", starts=DAYS[0], ends=DAYS[2])])
    await _pay(db, ev, a)
    out_a = await get_event_access(_user(a["business_id"], "STAFF"), db)
    assert out_a["is_event_vendor"] is True and out_a["access_level"] == "TRADE" and out_a["reason"] == "PAID_DAY"
    assert len(out_a["events"]) == 1
    e = out_a["events"][0]
    assert (e["event_name"], e["stall_code"], e["category"]) == (ev["name"], "A1", "FOOD")
    assert [(s["status"], s["amount_cents"]) for s in e["subscriptions"]] == [("PAID", 20_000)]
    out_b = await get_event_access(_user(b["business_id"], "OWNER"), db)
    assert out_b["access_level"] == "CONFIGURE" and out_b["reason"] == "NO_PAID_COVER_TODAY"


async def test_event_access_endpoint_never_returns_another_businesses_data(db, bag):
    ev1, ev2 = await _event(db, bag), await _event(db, bag)
    a = (await _vendors(db, ev1, bag, [_row(bag, stall="TENANT-A", starts=DAYS[0], ends=DAYS[0])]))[0]
    b = (await _vendors(db, ev2, bag, [_row(bag, stall="TENANT-B", starts=DAYS[0], ends=DAYS[1])]))[0]
    out = await get_event_access(_user(a["business_id"]), db)
    blob = repr(out)
    assert ev2["name"] not in blob and "TENANT-B" not in blob and str(b["business_id"]) not in blob
    assert [e["event_name"] for e in out["events"]] == [ev1["name"]]
    assert [len(e["subscriptions"]) for e in out["events"]] == [1]


async def test_event_access_endpoint_for_a_non_event_business(db, bag):
    from shared.utils import generate_business_code
    u = uuid.uuid4().hex[:8]
    biz = Business(name=f"Plain {u}", slug=f"plain-{u}", business_code=generate_business_code(), currency="ZAR",
                   timezone="Africa/Johannesburg")
    db.add(biz)
    await db.commit()
    bag["extra_biz"].append(biz.id)
    out = await get_event_access(_user(biz.id), db)
    assert out == {"is_event_vendor": False, "access_level": None, "reason": None, "events": []}


async def test_returning_vendor_at_a_cancelled_event_is_none(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    row = (await db.execute(select(Event).where(Event.id == ev["id"]))).scalar_one()
    row.status = "CANCELLED"
    await db.commit()
    out = await get_event_access(_user(v["business_id"]), db)
    assert (out["access_level"], out["reason"]) == ("NONE", "CANCELLED")
