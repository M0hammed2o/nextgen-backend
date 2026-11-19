"""
Souk WhatsApp gate, non-bot paths (real Postgres): payment link and delivery
fee gated, provider payment flagged (not gated), connection routes, admin
plane, order-ready template, event-access readiness, consent provenance.

No real message is sent: MetaGraphClient.send_message / send_notification_message
are patched wherever a send path is reachable. No template is created via API.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from admin_api.app.api.v1.routes_businesses import BusinessCreate, BusinessUpdate, create_business, update_business
from backend.app.api.v1 import routes_whatsapp_connect as wa_connect
from backend.app.api.v1.routes_business import get_event_access
from backend.app.api.v1.routes_orders import (
    DeliveryFeeRequest,
    StatusUpdateRequest,
    set_delivery_fee,
    update_order_status,
)
from backend.app.api.v1.routes_payments import _mark_order_paid
from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.events import order_ready
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.order import Order, OrderEvent
from shared.models.whatsapp_connection import WhatsAppConnection
from tests.integration.test_event_whatsapp_gate_realdb import (  # noqa: F401  (fixture + helpers)
    PAID_NOON,
    UNPAID_NOON,
    event_clock,
    stalls,
)
from tests.integration.test_souk_security_review_realdb import _till_and_item  # noqa: F401

pytestmark = pytest.mark.asyncio

ADMIN = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


def _user(business_id, role="OWNER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


async def _order(db, business_id, **kw) -> Order:
    base = dict(business_id=business_id, order_number=f"BO-{uuid.uuid4().hex[:8].upper()}", status="NEW",
                payment_status="UNPAID", source="POS", total_cents=3000, subtotal_cents=3000, currency="ZAR")
    base.update(kw)
    o = Order(**base)
    db.add(o)
    await db.commit()
    return o


async def _fresh(db, model, id_):
    return (await db.execute(select(model).where(model.id == id_).execution_options(populate_existing=True))
            ).scalar_one()


async def _err(coro):
    with pytest.raises(AppError) as exc:
        await coro
    return exc.value.status_code, exc.value.code


def _no_redis():
    return patch("backend.app.db.session.get_redis", new=AsyncMock(side_effect=RuntimeError("no redis")))


# -- payment link / delivery fee -----------------------------------------------------

async def test_payment_link_accept_refused_on_unpaid_day_allowed_on_trade_day(db, stalls):
    st = await stalls.make(online_payment=True, cart=False)
    order = await _order(db, st.business_id, source="WHATSAPP", payment_required=True)
    with event_clock(UNPAID_NOON), _no_redis():
        assert await _err(update_order_status(order.id, StatusUpdateRequest(status="ACCEPTED"),
                                              _user(st.business_id, "STAFF"), db)) == (403, "EVENT_TRADING_NOT_ALLOWED")
    assert (await _fresh(db, Order, order.id)).status == "NEW"
    with event_clock(PAID_NOON), _no_redis(), \
         patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock()):
        out = await update_order_status(order.id, StatusUpdateRequest(status="ACCEPTED"),
                                        _user(st.business_id, "STAFF"), db)
    assert out.status == "ACCEPTED"


async def test_restaurant_accept_with_payment_required_unaffected(db, stalls):
    st = await stalls.make(plan="STARTER", connection=None, online_payment=True, cart=False)
    order = await _order(db, st.business_id, source="WHATSAPP", payment_required=True)
    with _no_redis(), patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock()), \
         patch("backend.app.events.trading.business_access_decision",
               new=AsyncMock(side_effect=AssertionError("restaurant gated"))):
        out = await update_order_status(order.id, StatusUpdateRequest(status="ACCEPTED"),
                                        _user(st.business_id, "STAFF"), db)
    assert out.status == "ACCEPTED"


async def test_set_delivery_fee_refused_on_unpaid_day(db, stalls):
    st = await stalls.make(delivery=True, cart=False)
    order = await _order(db, st.business_id, source="WHATSAPP", status="PENDING_DELIVERY_FEE",
                         order_mode="DELIVERY", customer_id=st.customer_id)
    with event_clock(UNPAID_NOON), _no_redis():
        assert await _err(set_delivery_fee(order.id, DeliveryFeeRequest(delivery_fee_cents=2000),
                                           _user(st.business_id, "STAFF"), db)) == (403, "EVENT_TRADING_NOT_ALLOWED")
    o = await _fresh(db, Order, order.id)
    assert (o.status, o.delivery_fee_cents) == ("PENDING_DELIVERY_FEE", 0)
    with event_clock(PAID_NOON), _no_redis(), \
         patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock()):
        out = await set_delivery_fee(order.id, DeliveryFeeRequest(delivery_fee_cents=2000),
                                     _user(st.business_id, "STAFF"), db)
    assert out.status == "FEE_SENT" and out.total_cents == 5000


# -- provider payment: recorded, flagged, never refused ------------------------------

@pytest.mark.parametrize("when,flagged", [(UNPAID_NOON, True), (PAID_NOON, False)])
async def test_provider_paid_on_unpaid_day_is_recorded_and_flagged(db, stalls, when, flagged):
    st = await stalls.make(online_payment=True, cart=False)
    order = await _order(db, st.business_id, source="WHATSAPP", payment_required=True, status="ACCEPTED")
    await db.get(Business, st.business_id)  # as the webhook route does (signature check)
    with event_clock(when):
        await _mark_order_paid(db, str(order.id), st.business_id, "yoco", provider_reference="chr_1")
    assert (await _fresh(db, Order, order.id)).payment_status == "PAID"
    reasons = (await db.execute(select(OrderEvent.reason).where(OrderEvent.order_id == order.id))).scalars().all()
    audits = (await db.execute(select(AuditEvent.action).where(
        AuditEvent.business_id == st.business_id, AuditEvent.target_id == order.id))).scalars().all()
    has_flag = any("non-trading day" in r for r in reasons)
    assert has_flag is flagged
    assert ("event.payment.outside_trading_day" in audits) is flagged


async def test_restaurant_provider_payment_never_flagged(db, stalls):
    st = await stalls.make(plan="STARTER", connection=None, cart=False)
    order = await _order(db, st.business_id, source="WHATSAPP", payment_required=True, status="ACCEPTED")
    with patch("backend.app.events.trading.business_access_decision",
               new=AsyncMock(side_effect=AssertionError("restaurant gated"))):
        await _mark_order_paid(db, str(order.id), st.business_id, "yoco")
    reasons = (await db.execute(select(OrderEvent.reason).where(OrderEvent.order_id == order.id))).scalars().all()
    assert (await _fresh(db, Order, order.id)).payment_status == "PAID"
    assert not any("non-trading" in r for r in reasons)


# -- connection routes ----------------------------------------------------------------

async def test_event_vendor_can_always_disconnect_even_while_disabled(db, stalls):
    st = await stalls.make(wa_enabled=False, cart=False)
    with patch("backend.app.whatsapp.graph_client.MetaGraphClient.unsubscribe_app", new=AsyncMock()):
        out = await wa_connect.disconnect_whatsapp(_user(st.business_id), db)
    assert out.status == "DISCONNECTED"
    biz = await _fresh(db, Business, st.business_id)
    assert biz.whatsapp_phone_number_id is None
    conn = (await db.execute(select(WhatsAppConnection).where(
        WhatsAppConnection.business_id == st.business_id))).scalar_one()
    assert conn.access_token_encrypted is None


async def test_event_vendor_disabled_cannot_connect_or_test(db, stalls):
    st = await stalls.make(wa_enabled=False, connection=None, cart=False)
    for call in (wa_connect.start_connect.__wrapped__(None, _user(st.business_id), db),
                 wa_connect.run_health_check(_user(st.business_id), db)):
        assert await _err(call) == (403, "EVENT_WHATSAPP_NOT_AVAILABLE")


async def test_restaurant_disconnect_still_requires_module_enabled(db, stalls):
    st = await stalls.make(plan="STARTER", wa_enabled=False, cart=False)
    assert await _err(wa_connect.disconnect_whatsapp(_user(st.business_id), db)) == (409, "WHATSAPP_NOT_ENABLED")


# -- admin plane -----------------------------------------------------------------------

async def test_admin_ready_template_approval_rules(db, stalls):
    st = await stalls.make(cart=False)
    assert await _err(update_business(st.business_id, BusinessUpdate(whatsapp_ready_template_approved=True),
                                      ADMIN, db)) == (422, "READY_TEMPLATE_NAME_REQUIRED")
    out = await update_business(st.business_id, BusinessUpdate(
        whatsapp_ready_template_name="order_ready_v1", whatsapp_ready_template_language="en_US",
        whatsapp_ready_template_approved=True), ADMIN, db)
    assert (out.whatsapp_ready_template_name, out.whatsapp_ready_template_approved) == ("order_ready_v1", True)
    out = await update_business(st.business_id, BusinessUpdate(whatsapp_ready_template_name="order_ready_v2"),
                                ADMIN, db)
    assert out.whatsapp_ready_template_approved is False  # renamed: needs re-confirmation
    audit = (await db.execute(select(AuditEvent).where(
        AuditEvent.business_id == st.business_id, AuditEvent.action == "admin.business.updated")
        .order_by(AuditEvent.created_at))).scalars().all()
    approvals = [a for a in audit if a.diff_json.get("whatsapp_ready_template_approved") is True]
    assert len(approvals) == 1 and approvals[0].actor_user_id == ADMIN.user_id  # who/when of the approval


async def test_onboarder_cannot_touch_the_ready_template():
    """ONBOARDER PATCHes are allow-listed; the template fields are super-admin only."""
    from admin_api.app.api.v1.routes_businesses import ONBOARDER_PATCH_ALLOWED_FIELDS
    assert not {"whatsapp_ready_template_name", "whatsapp_ready_template_language",
                "whatsapp_ready_template_approved"} & ONBOARDER_PATCH_ALLOWED_FIELDS


async def test_admin_moving_into_event_plan_never_enables_whatsapp_in_the_same_request(db, stalls):
    st = await stalls.make(plan="STARTER", connection=None, cart=False)
    out = await update_business(st.business_id, BusinessUpdate(
        plan="EVENT", whatsapp_phone_number_id=None, is_whatsapp_enabled=True), ADMIN, db)
    assert out.plan == "EVENT" and out.is_whatsapp_enabled is False


# -- order-ready template (BUG-0210) ----------------------------------------------------

def _graph(ok=True):
    return patch("backend.app.whatsapp.graph_client.MetaGraphClient.send_message",
                 new=AsyncMock(return_value={"messages": [{"id": "wamid.READY"}]} if ok else {}))


async def _ready_vendor(db, stalls, *, approved=True, **kw):
    st = await stalls.make(cart=False, **kw)
    await db.execute(update(Business).where(Business.id == st.business_id).values(
        whatsapp_ready_template_name="order_ready_v1" if approved is not None else None,
        whatsapp_ready_template_language="en_US", whatsapp_ready_template_approved=bool(approved)))
    await db.commit()
    return st


async def _ready(db, st, order):
    with _no_redis():
        await update_order_status(order.id, StatusUpdateRequest(status="READY"), _user(st.business_id, "STAFF"), db)
    return (await db.execute(select(OrderEvent.reason).where(OrderEvent.order_id == order.id))).scalars().all()


async def test_ready_template_sent_once_with_vendor_token_and_params(db, stalls):
    st = await _ready_vendor(db, stalls)
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234567", order_number="BO-READY1")
    with _graph() as send:
        reasons = await _ready(db, st, order)
        assert await order_ready.send_event_order_ready(db, await _fresh(db, Business, st.business_id),
                                                        await _fresh(db, Order, order.id), "27821234567") \
            == "already sent"
    assert send.await_count == 1
    pnid, token, payload = send.await_args.args
    assert (pnid, token) == (st.pnid, "test-vendor-token")  # the vendor's own connection, never the platform token
    assert payload["to"] == "27821234567" and payload["type"] == "template"
    assert payload["template"]["name"] == "order_ready_v1"
    assert payload["template"]["language"] == {"code": "en_US"}
    params = [p["text"] for p in payload["template"]["components"][0]["parameters"]]
    assert params[0] == "BO-READY1" and params[1].startswith("Spice ") and "Spring Souk" in params[2]
    assert order_ready.REASON_SENT_TEMPLATE in reasons


async def test_ready_not_sent_without_consent(db, stalls):
    st = await _ready_vendor(db, stalls)
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=False,
                         phone_number="0821234567")
    with _graph() as send:
        reasons = await _ready(db, st, order)
    send.assert_not_awaited()
    assert not any(r and "Order-ready" in r for r in reasons)


async def test_ready_not_sent_to_opted_out_customer(db, stalls):
    st = await _ready_vendor(db, stalls)
    db.add(Customer(business_id=st.business_id, wa_id="27821234500", opted_out=True))
    await db.commit()
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234500")
    with _graph() as send:
        reasons = await _ready(db, st, order)
    send.assert_not_awaited()
    assert order_ready.REASON_NOT_SENT_OPTED_OUT in reasons


async def test_no_template_outside_24h_not_sent_and_recorded(db, stalls):
    st = await _ready_vendor(db, stalls, approved=False)
    db.add(Customer(business_id=st.business_id, wa_id="27821234511",
                    last_message_at=datetime.now(timezone.utc) - timedelta(hours=30)))
    await db.commit()
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234511")
    with _graph() as send:
        reasons = await _ready(db, st, order)
    send.assert_not_awaited()
    assert order_ready.REASON_NOT_SENT_NO_TEMPLATE in reasons


async def test_no_template_within_24h_sends_free_text(db, stalls):
    st = await _ready_vendor(db, stalls, approved=False)
    db.add(Customer(business_id=st.business_id, wa_id="27821234522",
                    last_message_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    await db.commit()
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234522")
    with _graph() as send:
        reasons = await _ready(db, st, order)
    assert send.await_count == 1
    _, token, payload = send.await_args.args
    assert token == "test-vendor-token" and payload["type"] == "text"
    assert order_ready.REASON_SENT_FREE_TEXT in reasons


async def test_ready_not_sent_when_whatsapp_disabled(db, stalls):
    st = await _ready_vendor(db, stalls, wa_enabled=False)
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234533")
    with _graph() as send:
        reasons = await _ready(db, st, order)
    send.assert_not_awaited()
    assert order_ready.REASON_NOT_SENT_UNAVAILABLE in reasons


# -- event-access readiness ---------------------------------------------------------------

@pytest.mark.parametrize("kw,approved,state,available", [
    ({}, True, "READY", True),
    ({}, False, "TEMPLATE_PENDING", False),
    ({"wa_enabled": False}, True, "AWAITING_ADMIN_APPROVAL", False),
    ({"connection": None}, True, "NOT_CONNECTED", False),
    ({"connection": "NEEDS_ATTENTION"}, True, "NOT_CONNECTED", False),
])
async def test_event_access_reports_ready_notify_availability(db, stalls, kw, approved, state, available):
    st = await _ready_vendor(db, stalls, approved=approved, **kw)
    with event_clock(PAID_NOON):
        out = await get_event_access(_user(st.business_id, "STAFF"), db)
    assert out["ready_notify_available"] is available
    assert out["whatsapp"]["state"] == state
    assert out["whatsapp"]["suggested_template"]["name"] == "order_ready_v1"
    assert "token" not in str(out["whatsapp"]).lower()


# -- consent provenance -------------------------------------------------------------------

async def test_pos_consent_provenance_recorded(db, stalls):
    st = await stalls.make(plan="STARTER", connection=None, cart=False)
    await db.execute(update(Business).where(Business.id == st.business_id).values(pos_enabled=True))
    await db.commit()
    owner = _user(st.business_id)
    item, _ = await _till_and_item(st.business_id, owner)
    out = {}
    for consent in (True, False):
        o = await create_pos_order(POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)], payment_method="CASH",
            cash_tendered_cents=5000, customer_phone="0821112222", notify_when_ready=consent), owner, db)
        out[consent] = await _fresh(db, Order, o.id)
    assert out[True].notify_consent_by_user_id == owner.user_id and out[True].notify_consent_at is not None
    assert out[False].notify_consent_by_user_id is None and out[False].notify_consent_at is None


async def test_ready_send_path_raising_still_returns_the_ready_order(db, stalls):
    """Review L-3: a failure inside the ready send must not roll back/expire the
    committed status change; the route still returns the READY order."""
    st = await _ready_vendor(db, stalls)
    order = await _order(db, st.business_id, status="IN_PROGRESS", payment_status="PAID", notify_when_ready=True,
                         phone_number="0821234544")
    with _no_redis(), patch("backend.app.bot.whatsapp_sender.send_template_message",
                            new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await update_order_status(order.id, StatusUpdateRequest(status="READY"),
                                        _user(st.business_id, "STAFF"), db)
    assert out.status == "READY" and out.id == order.id
    reasons = (await db.execute(select(OrderEvent.reason).where(OrderEvent.order_id == order.id))).scalars().all()
    assert order_ready.REASON_SEND_FAILED in reasons
    assert (await _fresh(db, Order, order.id)).status == "READY"


# -- review L-4: template approval is per WABA/number ---------------------------------------

async def test_disconnect_clears_template_approval(db, stalls):
    st = await _ready_vendor(db, stalls)
    with patch("backend.app.whatsapp.graph_client.MetaGraphClient.unsubscribe_app", new=AsyncMock()):
        await wa_connect.disconnect_whatsapp(_user(st.business_id), db)
    assert (await _fresh(db, Business, st.business_id)).whatsapp_ready_template_approved is False


@pytest.mark.parametrize("same", [True, False])
async def test_claiming_a_different_number_clears_template_approval(db, stalls, same):
    from backend.app.whatsapp import connection_service
    st = await _ready_vendor(db, stalls)
    biz = await _fresh(db, Business, st.business_id)
    conn = (await db.execute(select(WhatsAppConnection).where(
        WhatsAppConnection.business_id == st.business_id))).scalar_one()
    await db.execute(update(Business).where(Business.id == st.business_id).values(
        whatsapp_business_account_id=conn.waba_id))
    await db.commit()
    biz = await _fresh(db, Business, st.business_id)
    if not same:
        conn.phone_number_id = f"8{uuid.uuid4().int % 10**12:012d}"
        conn.waba_id = f"w-new-{uuid.uuid4().hex[:6]}"
    outcome = await connection_service._claim_routing_number(db, biz, conn, None)
    assert outcome.success
    assert (await _fresh(db, Business, st.business_id)).whatsapp_ready_template_approved is same


async def test_admin_number_change_clears_template_approval(db, stalls):
    st = await _ready_vendor(db, stalls, plan="STARTER", connection=None)
    out = await update_business(st.business_id, BusinessUpdate(
        whatsapp_phone_number_id=f"7{uuid.uuid4().int % 10**12:012d}"), ADMIN, db)
    assert out.whatsapp_ready_template_approved is False


# -- review L-8 ------------------------------------------------------------------------------

async def test_onboarder_cannot_enable_whatsapp_for_an_event_business(db, stalls):
    from sqlalchemy import delete as sa_delete
    from shared.models.admin import AdminUser
    admin_row = AdminUser(email=f"onb-{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
                          display_name="Onboarder", role="ONBOARDER")
    db.add(admin_row)
    await db.commit()
    try:
        st = await stalls.make(wa_enabled=False, connection=None, cart=False)
        await db.execute(update(Business).where(Business.id == st.business_id).values(
            onboarded_by_admin_id=admin_row.id))
        await db.commit()
        onb = AuthUser(user_id=admin_row.id, business_id=None, role="ONBOARDER", token_type="admin_access")
        assert await _err(update_business(st.business_id, BusinessUpdate(is_whatsapp_enabled=True), onb, db)) \
            == (403, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
        assert (await _fresh(db, Business, st.business_id)).is_whatsapp_enabled is False
    finally:
        await db.execute(update(Business).where(Business.onboarded_by_admin_id == admin_row.id).values(
            onboarded_by_admin_id=None))
        await db.execute(sa_delete(AdminUser).where(AdminUser.id == admin_row.id))
        await db.commit()


async def test_admin_platform_test_send_refuses_an_event_business(db, stalls):
    from admin_api.app.api.v1.routes_businesses import WhatsAppTestSendRequest, send_whatsapp_test
    st = await stalls.make(cart=False)
    with patch("backend.app.bot.whatsapp_sender._send_via_meta_api_with_detail", new=AsyncMock()) as send:
        assert await _err(send_whatsapp_test(st.business_id, WhatsAppTestSendRequest(to="27821234567", text="hi"),
                                             ADMIN, db)) == (422, "EVENT_PLAN_PLATFORM_TOKEN_NOT_ALLOWED")
    send.assert_not_awaited()
