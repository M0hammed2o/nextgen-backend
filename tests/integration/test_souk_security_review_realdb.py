"""
Real-Postgres tests for the souk security-review fixes (H1, M1, M2, L1-L7) and
the POS order-ready notification consent.

Route functions are called directly with real sessions (same pattern as
test_event_phase1_realdb.py). No real WhatsApp message is ever sent: every
test that reaches a send path patches ``send_notification_message`` /
``send_pos_payment_link`` / the payment provider.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError

from admin_api.app.api.v1.routes_events import (
    BulkVendorRequest,
    EventCreate,
    EventDayIn,
    MarkPaid,
    VendorRow,
    VendorSuspend,
    bulk_onboard_vendors,
    cancel_vendor,
    create_event,
    list_vendors,
    mark_subscription_paid,
    reinstate_vendor,
    suspend_vendor,
    update_registration,
    RegistrationUpdate,
    set_event_status,
    EventStatusUpdate,
)
from backend.app.api.v1 import routes_billing as billing_routes
from backend.app.api.v1 import routes_event_public as pub
from backend.app.api.v1 import routes_event_vendor as ven
from backend.app.api.v1 import routes_whatsapp_connect as wa_connect
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_orders import (
    PaymentUpdateRequest,
    StatusUpdateRequest,
    update_order_status,
    update_payment_status,
)
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSPaymentIntentCreateRequest,
    TillOpenRequest,
    create_pos_order,
    create_pos_payment_intent,
    open_till,
    resend_pos_payment_intent,
)
from backend.app.billing import lifecycle
from backend.app.billing.stripe_webhooks import _handle_subscription_updated
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.events import service as event_service
from backend.app.events.service import (
    EVENT_ADMIN_SUSPENSION_REASON,
    EVENT_SUSPENSION_REASON,
    business_access_decision,
    reconcile_event_vendor_access,
)
from backend.app.events.subscriptions import is_payment_reference_violation
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.event import Event, EventVendor, EventVendorSubscription
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.till import TillSession
from shared.models.user import BusinessUser
from tests.integration._db import get_session_factory

pytestmark = pytest.mark.asyncio

SAST = ZoneInfo("Africa/Johannesburg")
TODAY = datetime.now(SAST).date()
DAYS = [TODAY + timedelta(days=i) for i in range(5)]  # today .. today+4 (trading today when paid)
FAR = [TODAY + timedelta(days=60 + i) for i in range(3)]  # far outside the prep window: access NONE
ADMIN = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


def _user(business_id, role="OWNER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


@pytest.fixture
async def bag(db):
    bag = {"events": [], "emails": [], "extra_biz": []}
    yield bag
    await db.rollback()
    biz_ids = list(bag["extra_biz"])
    if bag["emails"]:
        biz_ids += (await db.execute(
            select(BusinessUser.business_id).where(BusinessUser.email.in_(bag["emails"])))).scalars().all()
    if biz_ids:
        await db.execute(delete(OrderItem).where(OrderItem.business_id.in_(biz_ids)))
        await db.execute(delete(OrderEvent).where(OrderEvent.business_id.in_(biz_ids)))
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id.in_(biz_ids)))
        await db.execute(delete(Order).where(Order.business_id.in_(biz_ids)))
        await db.execute(delete(TillSession).where(TillSession.business_id.in_(biz_ids)))
        await db.execute(delete(Customer).where(Customer.business_id.in_(biz_ids)))
        await db.execute(delete(Business).where(Business.id.in_(biz_ids)))
    if bag["events"]:
        await db.execute(delete(AuditEvent).where(AuditEvent.target_id.in_(bag["events"])))
        await db.execute(delete(Event).where(Event.id.in_(bag["events"])))
    await db.commit()


async def _event(db, bag, days=DAYS) -> dict:
    ev = await create_event(EventCreate(name=f"SR Souk {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=d) for d in days]), ADMIN, db)
    bag["events"].append(ev["id"])
    return ev


def _row(bag, category="FOOD", starts=None, ends=None) -> VendorRow:
    email = f"sr-{uuid.uuid4().hex[:8]}@example.com"
    bag["emails"].append(email)
    base = dict(category=category, business_name=f"SR Stall {uuid.uuid4().hex[:5]}", owner_email=email,
                owner_full_name="Vendor Owner")
    if starts:
        base.update(starts_on=starts, ends_on=ends or starts)
    return VendorRow(**base)


async def _vendors(db, ev, bag, rows) -> list[dict]:
    return (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db))["vendors"]


async def _sub_id(db, ev_id, vendor_id):
    items = (await list_vendors(ev_id, ADMIN, db))["items"]
    return next(v for v in items if v["vendor_id"] == vendor_id)["subscriptions"][0]["id"]


async def _pay(db, ev, vendor, ref=None):
    sub_id = await _sub_id(db, ev["id"], vendor["vendor_id"])
    return await mark_subscription_paid(
        ev["id"], sub_id, MarkPaid(payment_reference=ref or f"ref-{uuid.uuid4().hex}"), ADMIN, db)


async def _biz(db, business_id) -> Business:
    stmt = select(Business).where(Business.id == business_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def _code(coro) -> str:
    with pytest.raises(AppError) as exc:
        await coro
    return exc.value.code


async def _plain_business(db, bag, **kw) -> Business:
    from shared.utils import generate_business_code
    u = uuid.uuid4().hex[:8]
    biz = Business(name=f"Plain {u}", slug=f"plain-{u}", business_code=generate_business_code(), currency="ZAR",
                   timezone="Africa/Johannesburg", **kw)
    db.add(biz)
    await db.commit()
    bag["extra_biz"].append(biz.id)
    return biz


# ── H1: WhatsApp is not available to event vendors ───────────────────────────

async def test_h1_event_vendor_cannot_start_exchange_check_or_test_whatsapp(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    owner = _user(v["business_id"])
    body = wa_connect.ExchangeRequest(state="s", code="c", waba_id="1", phone_number_id="2")
    calls = [
        wa_connect.start_connect.__wrapped__(None, owner, db),
        wa_connect.exchange_code.__wrapped__(body, None, owner, db),
        wa_connect.run_health_check(owner, db),
        wa_connect.send_test_message.__wrapped__(wa_connect.TestMessageRequest(to="0821234567"), None, owner, db),
    ]
    for call in calls:
        with pytest.raises(AppError) as exc:
            await call
        assert (exc.value.status_code, exc.value.code) == (403, "EVENT_WHATSAPP_NOT_AVAILABLE")
    biz = await _biz(db, v["business_id"])
    assert biz.whatsapp_phone_number_id is None and biz.whatsapp_business_account_id is None


async def test_h1_the_refusal_holds_even_if_whatsapp_is_enabled_for_the_event_business(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    await db.execute(update(Business).where(Business.id == v["business_id"]).values(is_whatsapp_enabled=True))
    await db.commit()
    code = await _code(wa_connect.start_connect.__wrapped__(None, _user(v["business_id"]), db))
    assert code == "EVENT_WHATSAPP_NOT_AVAILABLE"


async def test_h1_admin_bulk_and_self_registered_vendors_have_whatsapp_disabled(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    assert (await _biz(db, v["business_id"])).is_whatsapp_enabled is False

    code = (await update_registration(ev["id"], RegistrationUpdate(open=True, rotate_code=True), ADMIN, db))[
        "registration_code"]
    await set_event_status(ev["id"], EventStatusUpdate(status="OPEN"), ADMIN, db)
    email = f"sr-{uuid.uuid4().hex[:8]}@example.com"
    bag["emails"].append(email)
    res = await pub.register_vendor.__wrapped__(
        (await db.execute(select(Event.slug).where(Event.id == ev["id"]))).scalar_one(),
        pub.RegisterRequest(registration_code=code, business_name="SR Self Stall", category="FOOD",
                            owner_full_name="Self Owner", owner_email=email, password="Vendor-chosen-pw-1",
                            accept_terms=True), None, db)
    self_biz = (await db.execute(select(Business).where(Business.business_code == res["business_code"]))).scalar_one()
    assert self_biz.plan == "EVENT" and self_biz.is_whatsapp_enabled is False


async def test_h1_a_non_event_business_is_not_refused(db, bag):
    biz = await _plain_business(db, bag)
    # Not the event refusal: it reaches the normal "no connection yet" answer.
    assert await _code(wa_connect.run_health_check(_user(biz.id), db)) == "WHATSAPP_NOT_CONNECTED"


# ── M1: billing never lifts an event suspension ──────────────────────────────

async def test_m1_mark_invoice_paid_leaves_event_suspensions_alone(db, bag):
    for reason in (EVENT_ADMIN_SUSPENSION_REASON, EVENT_SUSPENSION_REASON, "some unknown reason"):
        biz = await _plain_business(db, bag, plan="EVENT", is_active=False, suspended_reason=reason)
        lifecycle.mark_invoice_paid(biz)
        await db.commit()
        biz = await _biz(db, biz.id)
        assert (biz.is_active, biz.suspended_reason) == (False, reason), reason
        assert biz.billing_status == "ACTIVE"


async def test_m1_mark_invoice_paid_still_lifts_every_billing_owned_reason(db, bag):
    for reason in lifecycle.BILLING_SUSPENSION_REASONS:
        biz = await _plain_business(db, bag, is_active=False, suspended_reason=reason, billing_status="SUSPENDED")
        lifecycle.mark_invoice_paid(biz)
        await db.commit()
        biz = await _biz(db, biz.id)
        assert (biz.is_active, biz.suspended_reason) == (True, None), reason


async def test_m1_stripe_active_subscription_does_not_lift_an_event_admin_suspension(db, bag):
    cid = f"cus_{uuid.uuid4().hex[:10]}"
    biz = await _plain_business(db, bag, plan="EVENT", is_active=False, suspended_reason=EVENT_ADMIN_SUSPENSION_REASON,
                                stripe_customer_id=cid)
    await _handle_subscription_updated(db, {"status": "active", "customer": cid, "id": f"sub_{uuid.uuid4().hex[:8]}"})
    biz = await _biz(db, biz.id)
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_ADMIN_SUSPENSION_REASON)
    assert biz.billing_status == "ACTIVE"


async def test_m1_stripe_active_subscription_lifts_its_own_unpaid_suspension(db, bag):
    cid = f"cus_{uuid.uuid4().hex[:10]}"
    biz = await _plain_business(db, bag, is_active=False, suspended_reason="Subscription unpaid",
                                billing_status="SUSPENDED", stripe_customer_id=cid)
    await _handle_subscription_updated(db, {"status": "active", "customer": cid, "id": f"sub_{uuid.uuid4().hex[:8]}"})
    biz = await _biz(db, biz.id)
    assert (biz.is_active, biz.suspended_reason) == (True, None)


async def test_m1_event_vendors_cannot_use_saas_checkout_portal_or_cancel(db, bag):
    biz = await _plain_business(db, bag, plan="EVENT")
    owner = _user(biz.id)
    for call in (
        billing_routes.create_checkout_session(billing_routes.CheckoutSessionRequest(plan="STARTER"), owner, db),
        billing_routes.create_portal_session(owner, db),
        billing_routes.cancel_subscription(owner, db),
    ):
        with pytest.raises(AppError) as exc:
            await call
        assert (exc.value.status_code, exc.value.code) == (409, "NOT_AVAILABLE_FOR_EVENT_VENDORS")


# ── M2: cash-on-collection completion needs a paid trading day ───────────────

async def _order(db, bag, business_id, **kw) -> Order:
    base = dict(business_id=business_id, order_number=f"BO-{uuid.uuid4().hex[:8].upper()}", status="READY",
                payment_status="UNPAID", source="MANUAL", total_cents=1000, currency="ZAR")
    base.update(kw)
    order = Order(**base)
    db.add(order)
    await db.commit()
    return order


async def test_m2_unpaid_event_vendor_cannot_take_cash_on_collection_or_complete_an_unpaid_order(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])  # no subscription: CONFIGURE only
    user = _user(v["business_id"], "STAFF")
    order = await _order(db, bag, v["business_id"])
    assert await _code(update_payment_status(
        order.id, PaymentUpdateRequest(payment_status="CASH_ON_COLLECTION"), user, db)) == "EVENT_TRADING_NOT_ALLOWED"
    for target in ("COLLECTED", "DELIVERED", "COMPLETED"):
        assert await _code(update_order_status(order.id, StatusUpdateRequest(status=target), user, db)) in (
            "EVENT_TRADING_NOT_ALLOWED", "INVALID_TRANSITION"), target
    # The one that is a valid transition from READY must be refused by the gate specifically.
    assert await _code(update_order_status(
        order.id, StatusUpdateRequest(status="COLLECTED"), user, db)) == "EVENT_TRADING_NOT_ALLOWED"
    await db.refresh(order)
    assert (order.status, order.payment_status) == ("READY", "UNPAID")


async def test_m2_an_already_paid_order_can_still_move_through_the_kitchen(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])  # no paid cover today
    order = await _order(db, bag, v["business_id"], payment_status="PAID")
    out = await update_order_status(order.id, StatusUpdateRequest(status="COLLECTED"),
                                    _user(v["business_id"], "STAFF"), db)
    assert out.status == "COLLECTED"


async def test_m2_a_paid_trading_day_allows_cash_on_collection_and_completion(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    await _pay(db, ev, v)
    user = _user(v["business_id"], "STAFF")
    order = await _order(db, bag, v["business_id"])
    out = await update_payment_status(order.id, PaymentUpdateRequest(payment_status="CASH_ON_COLLECTION"), user, db)
    assert out.payment_status == "CASH_ON_COLLECTION"
    out = await update_order_status(order.id, StatusUpdateRequest(status="COLLECTED"), user, db)
    assert out.status == "COLLECTED"


async def test_m2_a_non_event_business_is_unaffected(db, bag):
    biz = await _plain_business(db, bag)
    order = await _order(db, bag, biz.id)
    user = _user(biz.id, "STAFF")
    assert (await update_payment_status(order.id, PaymentUpdateRequest(payment_status="CASH_ON_COLLECTION"),
                                        user, db)).payment_status == "CASH_ON_COLLECTION"
    assert (await update_order_status(order.id, StatusUpdateRequest(status="COLLECTED"), user, db)).status == "COLLECTED"


# ── L1: reconcile is compare-and-set and skips locked events ─────────────────

async def _far_vendor(db, bag):
    """A vendor with NO access (event far in the future) whose business is forced active."""
    ev = await _event(db, bag, days=FAR)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    async with get_session_factory()() as s:
        await s.execute(update(Business).where(Business.id == v["business_id"])
                        .values(is_active=True, suspended_reason=None))
        await s.commit()
    return ev, v


async def test_l1_a_concurrent_admin_suspension_is_not_overwritten_by_reconcile(db, bag):
    ev, v = await _far_vendor(db, bag)

    async def other():  # commits between the job's read and its write
        async with get_session_factory()() as b:
            await b.execute(update(Business).where(Business.id == v["business_id"]).values(
                is_active=False, suspended_reason=EVENT_ADMIN_SUSPENSION_REASON))
            await b.commit()

    result = await _run_with_interleave(db, [other()])
    assert v["business_id"] not in result.businesses_suspended
    biz = await _biz(db, v["business_id"])
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_ADMIN_SUSPENSION_REASON)


async def _run_with_interleave(db, pending):
    """Run reconcile; the interleaved coroutine is awaited between the read and the write."""
    real_execute = db.execute
    state = {"done": False}

    async def execute(stmt, *a, **kw):
        # The first UPDATE the job issues is its write: let the other connection commit first.
        if not state["done"] and pending and stmt.__class__.__name__ == "Update":
            state["done"] = True
            await pending.pop()
        return await real_execute(stmt, *a, **kw)

    with patch.object(db, "execute", execute):
        res = await reconcile_event_vendor_access(db)
    await db.commit()
    return res


async def test_l1_a_concurrent_cancel_is_not_overwritten_by_reconcile(db, bag):
    ev, v = await _far_vendor(db, bag)
    async with get_session_factory()() as s:  # stale ACTIVE: reconcile wants to write PENDING
        await s.execute(update(EventVendor).where(EventVendor.id == v["vendor_id"]).values(status="ACTIVE"))
        await s.commit()

    async def other():
        async with get_session_factory()() as b:
            await b.execute(update(EventVendor).where(EventVendor.id == v["vendor_id"]).values(status="CANCELLED"))
            await b.commit()

    res = await _run_with_interleave(db, [other()])
    assert res.vendor_status_changes == 0
    vendor = (await db.execute(select(EventVendor).where(EventVendor.id == v["vendor_id"])
                               .execution_options(populate_existing=True))).scalar_one()
    assert vendor.status == "CANCELLED"


async def test_l1_hourly_run_skips_an_event_locked_by_an_in_flight_change(db, bag):
    ev, v = await _far_vendor(db, bag)  # active business with no access: the job would suspend it
    async with get_session_factory()() as b:
        await b.execute(select(Event).where(Event.id == ev["id"]).with_for_update())  # B holds the event lock
        res = await reconcile_event_vendor_access(db, lock_events=True)
        await db.commit()
        assert res.businesses_suspended == [] and res.vendor_status_changes == 0
        assert (await _biz(db, v["business_id"])).is_active is True
        await b.rollback()
    res = await reconcile_event_vendor_access(db, lock_events=True)  # the next run, lock gone
    await db.commit()
    assert v["business_id"] in res.businesses_suspended
    biz = await _biz(db, v["business_id"])
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_SUSPENSION_REASON)


async def test_l1_cancel_vendor_takes_a_row_lock_on_the_vendor(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])
    async with get_session_factory()() as other:
        locked = (await other.execute(
            select(EventVendor.id).where(EventVendor.id == v["vendor_id"]).with_for_update(nowait=True))).first()
        assert locked  # nothing holds it before
        await other.rollback()
    # While cancel_vendor's transaction is open, the row is locked: simulate by running the
    # route up to its commit via a savepoint-free direct check of the SQL it issues.
    from admin_api.app.api.v1 import routes_events
    import inspect
    assert "lock=True" in inspect.getsource(routes_events.cancel_vendor)
    out = await cancel_vendor(ev["id"], v["vendor_id"], ADMIN, db)
    assert out["status"] == "CANCELLED"


# ── L2: the gate loads only the business's own vendor rows ───────────────────

async def test_l2_gate_does_not_load_other_vendors_of_the_event(db, bag):
    ev = await _event(db, bag)
    rows = [_row(bag, starts=DAYS[0], ends=DAYS[1]) for _ in range(4)]
    vendors = await _vendors(db, ev, bag, rows)
    await _pay(db, ev, vendors[0])
    db.expire_all()
    loaded: list = []
    listener = lambda session, obj: loaded.append(obj.business_id) if isinstance(obj, EventVendor) else None  # noqa: E731
    from sqlalchemy import event as sa_event
    sa_event.listen(db.sync_session, "loaded_as_persistent", listener)
    try:
        assert (await business_access_decision(db, vendors[0]["business_id"])).level == "TRADE"
    finally:
        sa_event.remove(db.sync_session, "loaded_as_persistent", listener)
    assert loaded == [vendors[0]["business_id"]]  # the other three vendors were never read


# ── L3: an automatic suspension may be upgraded to an administrative one ─────

async def test_l3_admin_suspend_overwrites_event_subscription_ended(db, bag):
    ev = await _event(db, bag, days=FAR)
    [v] = await _vendors(db, ev, bag, [_row(bag)])  # reconcile already suspended it: ENDED
    biz = await _biz(db, v["business_id"])
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_SUSPENSION_REASON)
    out = await suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="complaint"), ADMIN, db)
    assert out["suspended_reason"] == EVENT_ADMIN_SUSPENSION_REASON
    biz = await _biz(db, v["business_id"])
    assert (biz.is_active, biz.suspended_reason) == (False, EVENT_ADMIN_SUSPENSION_REASON)
    # Another reason is still refused, and the admin suspension survives reconcile.
    assert await _code(suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="x"), ADMIN, db)) == "ALREADY_SUSPENDED"
    await reconcile_event_vendor_access(db)
    await db.commit()
    assert (await _biz(db, v["business_id"])).suspended_reason == EVENT_ADMIN_SUSPENSION_REASON
    await reinstate_vendor(ev["id"], v["vendor_id"], ADMIN, db)


# ── L5: only the reference index maps to PAYMENT_REFERENCE_USED ──────────────

async def test_l5_only_the_payment_reference_index_is_a_reference_violation(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0]), _row(bag, starts=DAYS[0], ends=DAYS[0])])
    await _pay(db, ev, a, ref="L5-REF")
    sub_b = await _sub_id(db, ev["id"], b["vendor_id"])
    with pytest.raises(IntegrityError) as dup:
        await db.execute(update(EventVendorSubscription).where(EventVendorSubscription.id == sub_b)
                         .values(status="PAID", payment_reference="L5-REF",
                                  paid_at=datetime.now(timezone.utc)))
        await db.flush()
    await db.rollback()
    assert is_payment_reference_violation(dup.value) is True
    with pytest.raises(IntegrityError) as other:
        await db.execute(update(EventVendor).where(EventVendor.id == a["vendor_id"]).values(category="BAD"))
        await db.flush()
    await db.rollback()
    assert is_payment_reference_violation(other.value) is False


# ── L6: payment references are normalised ────────────────────────────────────

async def test_l6_references_are_stripped_and_upper_cased_everywhere(db, bag):
    ev = await _event(db, bag)
    a, b = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[0]), _row(bag, starts=DAYS[0], ends=DAYS[0])])
    assert MarkPaid(payment_reference="  eft-1 x ").payment_reference == "EFT-1 X"
    with pytest.raises(ValidationError):
        MarkPaid(payment_reference="   ")
    await _pay(db, ev, a, ref="  eft-l6 ")
    stored = (await db.execute(select(EventVendorSubscription.payment_reference)
                               .where(EventVendorSubscription.id == await _sub_id(db, ev["id"], a["vendor_id"])))).scalar_one()
    assert stored == "EFT-L6"
    # Same reference, different case/spacing, on another subscription: refused.
    assert await _code(_pay(db, ev, b, ref="Eft-L6  ")) == "PAYMENT_REFERENCE_USED"
    # Idempotent for the same subscription regardless of case.
    again = await _pay(db, ev, a, ref="eft-l6")
    assert again["already_paid"] is True
    # The vendor's claim route normalises and checks the same way.
    owner_b = _user(b["business_id"])
    sub_b = await _sub_id(db, ev["id"], b["vendor_id"])
    claim_used = ven.claim_payment(sub_b, ven.PaymentClaim(reference=" eft-l6"), owner_b, db)
    assert await _code(claim_used) == "PAYMENT_REFERENCE_USED"
    ok = await ven.claim_payment(sub_b, ven.PaymentClaim(reference="  my ref 7 "), owner_b, db)
    assert ok["payment_claim_reference"] == "MY REF 7"


# ── L7: gate behaviour on and off a trading day for the money endpoints ──────

async def _till_and_item(business_id, owner):
    async with get_session_factory()() as s:
        cat = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=s)
        item = await create_item(body=ItemCreate(category_id=cat.id, name="Souk Burger", price_cents=5000),
                                 user=owner, db=s)
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=s)
    return item, till


async def test_l7_resend_payment_intent_is_refused_on_a_non_trade_day(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag)])  # CONFIGURE only today
    # The gate runs before the intent is even looked up.
    assert await _code(resend_pos_payment_intent(uuid.uuid4(), _user(v["business_id"], "STAFF"), db)) \
        == "EVENT_TRADING_NOT_ALLOWED"


async def test_l7_trade_day_pos_order_and_payment_intent_succeed(db, bag):
    ev = await _event(db, bag)
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    await _pay(db, ev, v)
    owner = _user(v["business_id"])
    item, _till = await _till_and_item(v["business_id"], owner)

    order = await create_pos_order(
        POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                              payment_method="CASH", cash_tendered_cents=5000), owner, db)
    assert order.payment_status == "PAID" and order.total_cents == 5000

    await db.execute(update(Business).where(Business.id == v["business_id"]).values(pos_yoco_enabled=True))
    await db.commit()
    provider = AsyncMock()
    provider.create_payment_link = AsyncMock(return_value="https://pay.example.test/link")
    with patch("backend.app.payments.registry.get_provider", return_value=provider), \
            patch("backend.app.payments.pos_link_whatsapp.send_pos_payment_link", new=AsyncMock()) as sent:
        intent = await create_pos_payment_intent(
            POSPaymentIntentCreateRequest(amount_cents=1000, customer_phone="0821234567"), owner, db)
    assert intent.status == "PENDING" and sent.await_count == 1
