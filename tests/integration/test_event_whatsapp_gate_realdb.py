"""
Souk event-vendor WhatsApp trading gate: real Postgres, real pipeline.

process_inbound_message runs end to end against TEST_DATABASE_URL. Only the
outside world is mocked: the Meta send (whatsapp_sender.send_text_message),
Redis publish (SSE) and web push. No LLM is reachable on the paths tested
(asserted). The event clock is frozen by patching `datetime` in
backend.app.events.service / trading only, so "today" in the event's own
timezone is controlled exactly.

  export TEST_DATABASE_URL=postgresql+asyncpg://...
  export DATABASE_URL="$TEST_DATABASE_URL" && python -m alembic upgrade head
  python -m pytest tests/integration/test_event_whatsapp_gate_realdb.py -q
"""
from __future__ import annotations

import contextlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, event, func, select

from backend.app.bot import pipeline
from backend.app.bot.pipeline import process_inbound_message
from backend.app.bot.state_machine import add_to_cart
from backend.app.core.errors import AppError
from backend.app.inventory.ledger import stock_on_hand
from backend.app.inventory.units import to_milli
from shared.enums import EVENT_PLAN, ConversationState
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.event import Event, EventDay, EventVendor, EventVendorSubscription
from shared.models.inventory import Ingredient
from shared.models.message import Message
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderEvent
from shared.models.recipe import Recipe, RecipeItem
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code
from tests.integration._db import dispose_engine, get_engine, get_session_factory

pytestmark = pytest.mark.asyncio

TZ = "Asia/Tokyo"  # UTC+9: the event-local day differs from the UTC day for 9h of every day
PAID_DAY = date(2026, 12, 5)
UNPAID_DAY = PAID_DAY + timedelta(days=1)  # billable, no PAID cover -> CONFIGURE / NO_PAID_COVER_TODAY
NOTICE = "isn't taking WhatsApp orders right now"


def _local(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)


PAID_NOON = _local(PAID_DAY, 12, 0)
PAID_2355 = _local(PAID_DAY, 23, 55)
UNPAID_0005 = _local(UNPAID_DAY, 0, 5)
UNPAID_NOON = _local(UNPAID_DAY, 12, 0)


@contextlib.contextmanager
def event_clock(*times: datetime):
    """Each datetime.now() in the event modules takes the next value; the last one repeats."""
    seq = list(times)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            t = seq.pop(0) if len(seq) > 1 else seq[0]
            return t.astimezone(tz) if tz else t

    with patch("backend.app.events.service.datetime", Frozen), patch("backend.app.events.trading.datetime", Frozen):
        yield


@contextlib.contextmanager
def outside_world():
    redis = MagicMock()
    redis.publish = AsyncMock()
    with patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message",
               new=AsyncMock(return_value="wamid.SENT")) as send, \
         patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)), \
         patch("backend.app.db.session.get_redis", new=AsyncMock(return_value=redis)), \
         patch("backend.app.push_service.send_order_alert", new=AsyncMock()) as push, \
         patch.object(pipeline, "_handle_with_llm", new=AsyncMock(side_effect=AssertionError("LLM reached"))):
        yield SimpleNamespace(send=send, redis=redis, push=push)


def _sent_texts(world) -> list[str]:
    return [c.kwargs.get("text", "") for c in world.send.await_args_list]


# -- setup -------------------------------------------------------------------------

class Stall:
    """One vendor (or restaurant) with a menu item that consumes 100 g of stock."""

    def __init__(self, **kw):
        self.kw = kw
        self.business_ids: list[uuid.UUID] = []
        self.event_ids: list[uuid.UUID] = []

    async def make(self, *, plan=EVENT_PLAN, wa_enabled=True, connection="CONNECTED", paid=True,
                   delivery=False, online_payment=False, active=True, state=ConversationState.CONFIRMING_ORDER.value,
                   ctx_extra=None, cart=True):
        unique = uuid.uuid4().hex[:8]
        pnid = f"9{uuid.uuid4().int % 10**12:012d}"
        async with get_session_factory()() as db:
            biz = Business(
                name=f"Gate Stall {unique}", slug=f"gate-stall-{unique}", business_code=generate_business_code(),
                currency="ZAR", timezone="Africa/Johannesburg", plan=plan, is_active=active,
                is_whatsapp_enabled=wa_enabled, whatsapp_phone_number_id=pnid, inventory_enabled=True,
                delivery_enabled=delivery, require_customer_name=False, require_phone_number=False,
                online_payment_required=online_payment, brand_display_name=f"Spice {unique}",
            )
            db.add(biz)
            await db.flush()
            self.business_ids.append(biz.id)
            if plan == EVENT_PLAN:
                ev = Event(name=f"Spring Souk {unique}", slug=f"spring-souk-{unique}", timezone=TZ, status="OPEN",
                           food_day_rate_cents=10000, clothing_day_rate_cents=8000)
                db.add(ev)
                await db.flush()
                self.event_ids.append(ev.id)
                for d in (PAID_DAY, UNPAID_DAY):
                    db.add(EventDay(event_id=ev.id, day=d, is_billable=True))
                vendor = EventVendor(event_id=ev.id, business_id=biz.id, category="FOOD", status="ACTIVE")
                db.add(vendor)
                await db.flush()
                if paid:
                    db.add(EventVendorSubscription(
                        event_vendor_id=vendor.id, business_id=biz.id, starts_on=PAID_DAY, ends_on=PAID_DAY,
                        day_rate_cents=10000, billable_days=1, amount_cents=10000, status="PAID",
                        paid_at=datetime.now(timezone.utc)))
            if connection:
                db.add(WhatsAppConnection(business_id=biz.id, phone_number_id=pnid, waba_id=f"w{unique}",
                                          status=connection, access_token_encrypted="test-vendor-token"))
            ing = Ingredient(business_id=biz.id, name=f"Dough {unique}", canonical_unit="GRAM",
                             count_method="WEIGHT", cost_per_canonical_micro_cents=10_000_000)
            item = MenuItem(business_id=biz.id, name="Samoosa", price_cents=1500, currency="ZAR")
            db.add_all([ing, item])
            await db.flush()
            recipe = Recipe(business_id=biz.id, menu_item_id=item.id, yield_qty=1)
            db.add(recipe)
            await db.flush()
            db.add(RecipeItem(business_id=biz.id, recipe_id=recipe.id, ingredient_id=ing.id,
                              quantity_milli=to_milli(100, "g")))
            wa_id = f"2782{uuid.uuid4().int % 10**7:07d}"
            cust = Customer(business_id=biz.id, wa_id=wa_id)
            db.add(cust)
            await db.flush()
            sess = ConversationSession(business_id=biz.id, customer_id=cust.id, state=state, context_json={})
            if cart:
                add_to_cart(sess, menu_item_id=str(item.id), name=item.name, price_cents=item.price_cents, quantity=2)
            ctx = dict(sess.context_json or {})
            ctx.update({"order_mode": "PICKUP", "order_notes_asked": True})
            if delivery:
                ctx.update({"order_mode": "DELIVERY", "delivery_address": "12 Stall Row"})
            ctx.update(ctx_extra or {})
            sess.context_json = ctx
            db.add(sess)
            await db.commit()
            return SimpleNamespace(business_id=biz.id, pnid=pnid, wa_id=wa_id, customer_id=cust.id,
                                   ingredient_id=ing.id, item_id=item.id, session_id=sess.id)

    async def cleanup(self):
        async with get_session_factory()() as db:
            for bid in self.business_ids:
                await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
                await db.execute(delete(Message).where(Message.business_id == bid))
                await db.execute(delete(Business).where(Business.id == bid))
            for eid in self.event_ids:
                await db.execute(delete(Event).where(Event.id == eid))
            await db.commit()


@pytest.fixture
async def stalls():
    s = Stall()
    yield s
    await s.cleanup()
    await dispose_engine()  # per-test event loop (see tests/integration/conftest.py::db)


async def _send(st, text="yes"):
    async with get_session_factory()() as db:
        await process_inbound_message(db=db, phone_number_id=st.pnid, wa_message_id=f"wamid.{uuid.uuid4().hex}",
                                      wa_id=st.wa_id, msg_text=text, msg_type="text", raw_payload={},
                                      contact_name=None)


async def _snapshot(st):
    async with get_session_factory()() as db:
        orders = (await db.execute(select(Order).where(Order.business_id == st.business_id)
                                   .order_by(Order.created_at))).scalars().all()
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        inbound = (await db.execute(select(func.count()).select_from(Message).where(
            Message.business_id == st.business_id, Message.direction == "INBOUND"))).scalar_one()
        stock = (await stock_on_hand(db, st.business_id, ingredient_ids=[st.ingredient_id])).get(st.ingredient_id, 0)
        events = (await db.execute(select(OrderEvent).where(OrderEvent.business_id == st.business_id)
                                   .order_by(OrderEvent.created_at))).scalars().all()
        return SimpleNamespace(orders=orders, state=sess.state, ctx=sess.context_json or {}, inbound=inbound,
                               stock=stock, events=events)


# -- TRADE day ----------------------------------------------------------------------

async def test_trade_day_pickup_confirm_creates_order(stalls):
    st = await stalls.make()
    with event_clock(PAID_NOON), outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert [(o.status, o.source, o.total_cents) for o in snap.orders] == [("NEW", "WHATSAPP", 3000)]
    assert snap.stock == -to_milli(200, "g")
    assert snap.state == ConversationState.ORDER_PLACED.value
    w.redis.publish.assert_awaited()
    assert not any(NOTICE in t for t in _sent_texts(w))


async def test_trade_day_delivery_confirm_creates_pending_order(stalls):
    st = await stalls.make(delivery=True)
    with event_clock(PAID_NOON), outside_world():
        await _send(st)
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == ["PENDING_DELIVERY_FEE"]
    assert snap.state == ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value


# -- unpaid day ---------------------------------------------------------------------

async def test_unpaid_day_confirm_refused_nothing_created_or_published(stalls):
    st = await stalls.make()
    with event_clock(UNPAID_NOON), outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == []
    assert snap.stock == 0
    assert snap.inbound == 1  # the inbound message row is kept
    assert snap.state == ConversationState.IDLE.value
    assert snap.ctx.get("cart") == [] and "confirmed_cart" not in snap.ctx
    texts = _sent_texts(w)
    assert len(texts) == 1 and NOTICE in texts[0]
    assert "Spring Souk" in texts[0] and "not been charged" in texts[0]
    w.redis.publish.assert_not_awaited()
    w.push.assert_not_called()


async def test_cart_at_2355_paid_yes_at_0005_unpaid_is_refused_non_utc(stalls):
    """Soft gate sees 23:59:59 (TRADE), the confirmation's hard gate sees 00:00:01 next day."""
    st = await stalls.make()
    with event_clock(_local(PAID_DAY, 23, 59, 59), _local(UNPAID_DAY, 0, 0, 1)), outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.stock == 0
    assert NOTICE in _sent_texts(w)[0]
    w.redis.publish.assert_not_awaited()
    # 00:05 local is still the PREVIOUS UTC day (15:05Z on Dec 5): the event zone decides, not UTC.
    assert UNPAID_0005.date() == PAID_DAY
    st2 = await stalls.make()
    with event_clock(UNPAID_0005), outside_world():
        await _send(st2)
    assert (await _snapshot(st2)).orders == []
    st3 = await stalls.make()
    with event_clock(PAID_2355), outside_world():
        await _send(st3)
    assert [o.status for o in (await _snapshot(st3)).orders] == ["NEW"]


async def _paid_day_pending_delivery(stalls, **kw):
    st = await stalls.make(delivery=True, **kw)
    with event_clock(PAID_NOON), outside_world():
        await _send(st)
    # Staff set the fee on the paid day (what set_delivery_fee writes).
    async with get_session_factory()() as db:
        order = (await db.execute(select(Order).where(Order.business_id == st.business_id))).scalar_one()
        order.status, order.delivery_fee_cents, order.total_cents = "FEE_SENT", 2000, 5000
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        sess.context_json = {**(sess.context_json or {}), "delivery_fee_cents": 2000,
                             "delivery_fee_status": "FEE_SENT", "pending_order_id": str(order.id)}
        sess.state = ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value
        await db.commit()
    assert (await _snapshot(st)).stock == -to_milli(200, "g")
    return st


async def test_fee_approved_after_midnight_unpaid_cancels_order_and_reverses_stock(stalls):
    st = await _paid_day_pending_delivery(stalls)
    with event_clock(UNPAID_0005), outside_world() as w:
        await _send(st, "yes")
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == ["CANCELLED"]
    assert snap.stock == 0  # consumption reversed
    assert ("FEE_SENT", "CANCELLED") in [(e.old_status, e.new_status) for e in snap.events]
    assert snap.state == ConversationState.IDLE.value and "pending_order_id" not in snap.ctx
    assert NOTICE in _sent_texts(w)[0]
    # Review L-7: the staff dashboard is told, like any other cancellation.
    events = [json.loads(c.args[1]) for c in w.redis.publish.await_args_list]
    assert {"type": "order_status_changed", "order_id": str(snap.orders[0].id), "old_status": "FEE_SENT",
            "new_status": "CANCELLED"}.items() <= events[0].items()


async def test_cash_choice_after_midnight_hits_finalise_gate(stalls):
    """Soft gate still TRADE, then the day rolls over before _finalize_pending_delivery_order."""
    st = await _paid_day_pending_delivery(stalls)
    async with get_session_factory()() as db:
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        sess.state = ConversationState.COLLECTING_PAYMENT.value
        sess.context_json = {**sess.context_json, "delivery_fee_status": "APPROVED"}
        await db.commit()
    with event_clock(_local(PAID_DAY, 23, 59, 59), UNPAID_0005), outside_world() as w:
        await _send(st, "cash")
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == ["CANCELLED"] and snap.stock == 0
    assert NOTICE in _sent_texts(w)[0]


async def test_online_payment_fee_yes_after_midnight_refused(stalls):
    st = await _paid_day_pending_delivery(stalls, online_payment=True)
    with event_clock(_local(PAID_DAY, 23, 59, 59), UNPAID_0005), outside_world():
        await _send(st, "yes")
    snap = await _snapshot(st)
    assert [(o.status, o.payment_status) for o in snap.orders][0][0] == "CANCELLED"


@pytest.mark.parametrize("state,ctx,text", [
    (ConversationState.BUILDING_CART.value, {}, "add 2 samoosa"),
    (ConversationState.ORDER_PLACED.value, {}, "I want to order 3 samoosa"),
    (ConversationState.CHOOSING_ORDER_MODE.value, {"order_mode": None}, "pickup"),
    (ConversationState.COLLECTING_DETAILS.value, {}, "skip"),
    (ConversationState.BROWSING_MENU.value,
     {"recommended_items": [{"name": "Samoosa", "quantity": 2}]}, "yes please"),
])
async def test_every_entry_point_refused_on_unpaid_day_without_llm(stalls, state, ctx, text):
    st = await stalls.make(state=state, ctx_extra=ctx)
    with event_clock(UNPAID_NOON), outside_world() as w, \
         patch.object(pipeline, "_handle_message", new=AsyncMock(side_effect=AssertionError("handled"))):
        await _send(st, text)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.state == ConversationState.IDLE.value
    assert NOTICE in _sent_texts(w)[0]


async def test_refused_confirmation_does_not_cancel_superseded_order(stalls):
    st = await stalls.make()
    with event_clock(PAID_NOON), outside_world():
        await _send(st)  # a live NEW order on the paid day
    first = (await _snapshot(st)).orders[0]
    async with get_session_factory()() as db:
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        add_to_cart(sess, menu_item_id=str(st.item_id), name="Samoosa", price_cents=1500, quantity=1)
        sess.context_json = {**sess.context_json, "superseded_order_id": str(first.id), "order_mode": "PICKUP",
                             "order_notes_asked": True}
        sess.state = ConversationState.CONFIRMING_ORDER.value
        await db.commit()
    with event_clock(UNPAID_NOON), outside_world():
        await _send(st)
    snap = await _snapshot(st)
    assert [(o.id, o.status) for o in snap.orders] == [(first.id, "NEW")]
    assert "superseded_order_id" not in snap.ctx


async def test_notice_at_most_once_per_event_local_day(stalls):
    st = await stalls.make()
    with outside_world() as w:
        with event_clock(UNPAID_0005):
            await _send(st, "hi")
        with event_clock(UNPAID_NOON):
            await _send(st, "hello?")
        with event_clock(_local(UNPAID_DAY + timedelta(days=1), 9, 0)):  # after the event: NONE/EVENT_ENDED
            await _send(st, "anyone?")
    texts = _sent_texts(w)
    assert len(texts) == 2 and all(NOTICE in t for t in texts)
    assert (await _snapshot(st)).inbound == 3


async def test_access_check_error_never_escapes(stalls):
    st = await stalls.make()
    with event_clock(PAID_NOON), outside_world() as w, \
         patch("backend.app.events.trading.business_access_decision",
               new=AsyncMock(side_effect=AppError("BOOM", "db hiccup", 500))):
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.inbound == 1  # committed, not rolled back
    assert NOTICE in _sent_texts(w)[0]  # fail closed, customer still told


# -- WhatsApp unavailable for the vendor ---------------------------------------------

@pytest.mark.parametrize("kw", [
    {"wa_enabled": False},                    # number still set, kill switch off
    {"connection": "NEEDS_ATTENTION"},
    {"connection": "DISCONNECTED"},
    {"connection": None},                     # platform token would be the source -> refused
])
async def test_vendor_without_usable_whatsapp_processes_nothing(stalls, kw):
    st = await stalls.make(**kw)
    with event_clock(PAID_NOON), outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.inbound == 1
    assert snap.state == ConversationState.CONFIRMING_ORDER.value and len(snap.ctx["cart"]) == 1  # untouched
    w.send.assert_not_awaited()


async def test_suspended_vendor_is_dropped_silently(stalls):
    st = await stalls.make(active=False)
    with event_clock(PAID_NOON), outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.inbound == 0
    w.send.assert_not_awaited()


# -- restaurants unaffected ------------------------------------------------------------

@contextlib.contextmanager
def statements():
    seen: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    eng = get_engine().sync_engine
    event.listen(eng, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(eng, "before_cursor_execute", before)


@pytest.mark.parametrize("delivery", [False, True])
async def test_restaurant_orders_unaffected_even_if_event_gate_would_raise(stalls, delivery):
    st = await stalls.make(plan="STARTER", connection=None, delivery=delivery)
    boom = AsyncMock(side_effect=AssertionError("restaurant reached the event gate"))
    with outside_world() as w, statements() as sql, \
         patch("backend.app.events.trading.business_access_decision", new=boom), \
         patch("backend.app.events.trading._require_trade_at", new=boom), \
         patch.object(pipeline, "bot_event_context", new=boom):
        await _send(st)
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == (["PENDING_DELIVERY_FEE"] if delivery else ["NEW"])
    assert not any("event_vendors" in s or "SAVEPOINT" in s for s in sql)
    assert not any(NOTICE in t for t in _sent_texts(w))


# -- H-3 (restaurant-affecting fix) ------------------------------------------------------

async def test_h3_late_cash_does_not_resurrect_cancelled_order(stalls):
    st = await stalls.make(plan="STARTER", connection=None, delivery=True)
    with outside_world():
        await _send(st)
    async with get_session_factory()() as db:
        order = (await db.execute(select(Order).where(Order.business_id == st.business_id))).scalar_one()
        order.status = "CANCELLED"  # e.g. the 45-minute fee timeout
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        sess.state = ConversationState.COLLECTING_PAYMENT.value
        sess.context_json = {**sess.context_json, "pending_order_id": str(order.id), "delivery_fee_cents": 2000,
                             "delivery_fee_status": "APPROVED"}
        await db.commit()
    with outside_world() as w:
        await _send(st, "cash")
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == ["CANCELLED"]
    assert "expired or was cancelled" in _sent_texts(w)[0]
    assert snap.state == ConversationState.IDLE.value


async def test_h3_finalise_keeps_vat_total_set_by_set_delivery_fee(stalls):
    st = await stalls.make(plan="STARTER", connection=None, delivery=True)
    with outside_world():
        await _send(st)
    async with get_session_factory()() as db:
        order = (await db.execute(select(Order).where(Order.business_id == st.business_id))).scalar_one()
        # set_delivery_fee on a 15% VAT-exclusive business: (3000 + 2000) * 1.15
        order.status, order.delivery_fee_cents, order.total_cents, order.tax_cents = "FEE_SENT", 2000, 5750, 750
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        sess.state = ConversationState.COLLECTING_PAYMENT.value
        sess.context_json = {**sess.context_json, "pending_order_id": str(order.id), "delivery_fee_cents": 2000,
                             "delivery_fee_status": "APPROVED"}
        await db.commit()
    with outside_world() as w:
        await _send(st, "cash")
    snap = await _snapshot(st)
    o = snap.orders[0]
    assert (o.status, o.total_cents, o.tax_cents, o.delivery_fee_cents) == ("NEW", 5750, 750, 2000)
    assert "R57.50" in _sent_texts(w)[0]


async def test_h3_repeat_yes_for_an_already_live_order_says_placed_not_expired(stalls):
    """Review L-1: the order went FEE_SENT -> NEW elsewhere (staff, or a concurrent "yes")."""
    st = await stalls.make(plan="STARTER", connection=None, delivery=True)
    with outside_world():
        await _send(st)
    async with get_session_factory()() as db:
        order = (await db.execute(select(Order).where(Order.business_id == st.business_id))).scalar_one()
        order.status = "NEW"
        sess = (await db.execute(select(ConversationSession).where(
            ConversationSession.id == st.session_id))).scalar_one()
        sess.state = ConversationState.COLLECTING_PAYMENT.value
        sess.context_json = {**sess.context_json, "pending_order_id": str(order.id), "delivery_fee_cents": 2000,
                             "delivery_fee_status": "APPROVED"}
        await db.commit()
        number = order.order_number
    with outside_world() as w:
        await _send(st, "cash")
    snap = await _snapshot(st)
    assert [o.status for o in snap.orders] == ["NEW"]
    text = _sent_texts(w)[0]
    assert "already been placed" in text and number in text and "expired" not in text
    assert snap.state == ConversationState.ORDER_PLACED.value


@pytest.mark.parametrize("delivery", [False, True])
async def test_backstop_refusal_in_midnight_race_replies_and_commits(stalls, delivery):
    """Review L-2: soft gate and gate (a) see 23:59:5x (TRADE); the backstop in
    create_order_from_cart sees 00:00:01 (unpaid). The customer gets the notice,
    the inbound message is committed, nothing is ordered."""
    st = await stalls.make(delivery=delivery)
    with event_clock(_local(PAID_DAY, 23, 59, 58), _local(PAID_DAY, 23, 59, 59), _local(UNPAID_DAY, 0, 0, 1)), \
            outside_world() as w:
        await _send(st)
    snap = await _snapshot(st)
    assert snap.orders == [] and snap.stock == 0
    assert snap.inbound == 1  # not rolled back
    assert snap.state == ConversationState.IDLE.value and snap.ctx.get("cart") == []
    assert NOTICE in _sent_texts(w)[0]
    w.redis.publish.assert_not_awaited()
