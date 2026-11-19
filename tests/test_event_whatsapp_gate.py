"""
Souk event-vendor WhatsApp trading gate (mocked unit tier).

Real-Postgres end-to-end proof (process_inbound_message, Order rows, stock,
timezones): tests/integration/test_event_whatsapp_gate_realdb.py.

What is pinned here:
- trading.bot_trade_refusal / assert_bot_order_allowed: restaurants return
  before any SQL; event vendors get the live decision; never raises.
- hard gate (a) _handle_order_confirmation, (b) _finalize_pending_delivery_order,
  (c) order_creator.create_order_from_cart backstop.
- soft gate in _process: WA off / no credentials -> nothing processed;
  not trading -> notice once per event-local day, no LLM, cart cleared.
- restaurants: the gate/access functions patched to RAISE, yet orders are
  still created on every path (the gate never reaches them).
"""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.bot import order_creator, pipeline, state_machine
from backend.app.core.errors import AppError
from backend.app.events import trading
from backend.app.events.rules import CONFIGURE, NONE, TRADE, AccessDecision
from shared.enums import EVENT_PLAN, ConversationState
from shared.models.order import OrderEvent

pytestmark = pytest.mark.asyncio

NOTICE_START = "Sorry, *Spice Stall* isn't taking WhatsApp orders right now."


# -- fakes ---------------------------------------------------------------------

class FakeDB:
    """Records execute() calls; begin_nested() is a working async context manager."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.execute = AsyncMock(side_effect=self._execute)
        self.add = MagicMock()
        self.flush = AsyncMock()
        self.nested = 0

    async def _execute(self, stmt, *a, **k):
        if self.results:
            return self.results.pop(0)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        r.scalar_one.return_value = 1
        r.one_or_none.return_value = None
        return r

    @contextlib.asynccontextmanager
    async def _nested(self):
        self.nested += 1
        yield

    def begin_nested(self):
        return self._nested()


def _result(scalar=None, one=None):
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar
    r.scalar_one.return_value = scalar
    r.one_or_none.return_value = one
    return r


class Session:
    def __init__(self, state=ConversationState.CONFIRMING_ORDER.value, ctx=None):
        self.id = uuid.uuid4()
        self.state = state
        self.context_json = dict(ctx or {})


def _biz(plan=EVENT_PLAN, **kw):
    b = SimpleNamespace(
        id=uuid.uuid4(), plan=plan, name="Spice Stall Pty", brand_display_name="Spice Stall",
        currency="ZAR", timezone="Africa/Johannesburg", is_active=True, is_whatsapp_enabled=True,
        whatsapp_phone_number_id="pnid-1", delivery_enabled=False, order_in_only=False,
        require_customer_name=False, require_phone_number=False, online_payment_required=False,
        inventory_enabled=False, business_hours=None, whatsapp_paused=False,
    )
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _cart():
    return [{"menu_item_id": str(uuid.uuid4()), "name": "Samoosa", "price_cents": 1500,
             "quantity": 2, "line_total_cents": 3000}]


def _ctx(**kw):
    return trading.BotEventContext(event_name=kw.get("name", "Spring Souk"), local_day=kw.get("day", "2026-11-20"))


@contextlib.contextmanager
def refused(reason="NO_PAID_COVER_TODAY"):
    with patch.object(pipeline, "bot_trade_refusal", AsyncMock(return_value=reason)) as r, \
         patch.object(pipeline, "bot_event_context", AsyncMock(return_value=_ctx())):
        yield r


# -- trading helpers -------------------------------------------------------------

async def test_restaurant_refusal_check_issues_no_sql():
    db = FakeDB()
    with patch.object(trading, "business_access_decision", AsyncMock(side_effect=AssertionError("queried"))):
        assert await trading.bot_trade_refusal(db, _biz(plan="STARTER")) is None
        await trading.assert_bot_order_allowed(db, _biz(plan="GROWTH"))
    db.execute.assert_not_awaited()
    assert db.nested == 0


@pytest.mark.parametrize("level,reason,expected", [
    (TRADE, "PAID_DAY", None),
    (CONFIGURE, "NO_PAID_COVER_TODAY", "NO_PAID_COVER_TODAY"),
    (CONFIGURE, "PREP_WINDOW", "PREP_WINDOW"),
    (NONE, "NO_EVENT", "NO_EVENT"),
])
async def test_event_refusal_follows_live_access_decision(level, reason, expected):
    with patch.object(trading, "business_access_decision", AsyncMock(return_value=AccessDecision(level, reason))):
        assert await trading.bot_trade_refusal(FakeDB(), _biz()) == expected


async def test_event_refusal_never_raises_and_fails_closed():
    with patch.object(trading, "business_access_decision", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await trading.bot_trade_refusal(FakeDB(), _biz()) == trading.BOT_ACCESS_CHECK_FAILED
    with patch.object(trading, "business_access_decision",
                      AsyncMock(side_effect=AppError("X", "boom", 403))):
        assert await trading.bot_trade_refusal(FakeDB(), _biz()) == trading.BOT_ACCESS_CHECK_FAILED


# -- backstop (c) ------------------------------------------------------------------

async def test_backstop_raises_before_any_row_or_sse_for_event_vendor():
    db = FakeDB()
    session = Session(ctx={"cart": _cart()})
    with patch.object(trading, "business_access_decision",
                      AsyncMock(return_value=AccessDecision(CONFIGURE, "NO_PAID_COVER_TODAY"))), \
         patch("backend.app.db.session.get_redis", AsyncMock()) as redis, \
         patch("backend.app.push_service.send_order_alert", AsyncMock()) as push:
        with pytest.raises(AppError) as exc:
            await order_creator.create_order_from_cart(db, _biz(), MagicMock(id=uuid.uuid4()), session)
    assert exc.value.code == "EVENT_TRADING_NOT_ALLOWED"
    db.execute.assert_not_awaited()  # no order-number bump, no insert
    db.add.assert_not_called()
    redis.assert_not_awaited()
    push.assert_not_called()


async def test_backstop_allows_event_vendor_on_trade_day_and_restaurant_never_checks():
    for biz, decision in ((_biz(), AsyncMock(return_value=AccessDecision(TRADE, "PAID_DAY"))),
                          (_biz(plan="STARTER"), AsyncMock(side_effect=AssertionError("restaurant gated")))):
        db = FakeDB([_result(scalar=7)])
        session = Session(ctx={"cart": _cart()})
        with patch.object(trading, "business_access_decision", decision), \
             patch("backend.app.db.session.get_redis", AsyncMock(side_effect=RuntimeError("no redis"))), \
             patch("backend.app.push_service.send_order_alert", AsyncMock()):
            order = await order_creator.create_order_from_cart(db, biz, MagicMock(id=uuid.uuid4()), session)
        assert order.total_cents == 3000 and order.order_number


# -- hard gate (a) -------------------------------------------------------------------

async def test_confirmation_refused_no_order_no_superseded_cancel():
    biz = _biz()
    superseded = str(uuid.uuid4())
    session = Session(ctx={"cart": _cart(), "confirmed_cart": _cart(), "order_mode": "PICKUP",
                           "superseded_order_id": superseded, "order_notes_asked": True})
    with refused(), \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()) as cancel_superseded, \
         patch.object(pipeline, "_cancel_prior_live_orders", AsyncMock()) as cancel_prior, \
         patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock()) as create, \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()) as limit:
        text, is_llm, *_ = await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)
    assert text.startswith(NOTICE_START)
    assert "Spring Souk" in text and "not been charged" in text
    assert "paid" not in text.lower().replace("not been charged", "")
    assert is_llm is False
    create.assert_not_awaited()
    cancel_superseded.assert_not_awaited()
    cancel_prior.assert_not_awaited()
    limit.assert_not_awaited()
    assert state_machine.get_cart(session) == []
    assert state_machine.get_context(session, "superseded_order_id") is None  # forgotten, not cancelled
    assert state_machine.get_context(session, "confirmed_cart") is None
    assert session.state == ConversationState.IDLE.value


async def test_delivery_confirmation_refused_before_pending_order_created():
    biz = _biz(delivery_enabled=True)
    session = Session(ctx={"cart": _cart(), "order_mode": "DELIVERY", "delivery_address": "1 Main Rd",
                           "order_notes_asked": True})
    with refused(), patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock()) as create, \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()) as cancel_superseded:
        text, *_ = await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)
    assert text.startswith(NOTICE_START)
    create.assert_not_awaited()
    cancel_superseded.assert_not_awaited()


@pytest.mark.parametrize("entry", ["choosing_mode_pickup", "choosing_mode_delivery", "collecting_details"])
async def test_every_path_into_confirmation_is_refused(entry):
    biz = _biz(delivery_enabled=True)
    session = Session(ctx={"cart": _cart(), "order_notes_asked": True, "customer_name": "A",
                           "phone_number": "0820000000", "delivery_address": "1 Main Rd"})
    with refused(), patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock()) as create:
        if entry == "choosing_mode_pickup":
            text, *_ = await pipeline._handle_choosing_order_mode(FakeDB(), biz, MagicMock(), session, "pickup")
        elif entry == "choosing_mode_delivery":
            text, *_ = await pipeline._handle_choosing_order_mode(FakeDB(), biz, MagicMock(), session, "delivery")
        else:
            session.state = ConversationState.COLLECTING_DETAILS.value
            state_machine.set_context(session, "order_mode", "PICKUP")
            text, *_ = await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)
    assert text.startswith(NOTICE_START)
    create.assert_not_awaited()


# -- hard gate (b) -------------------------------------------------------------------

async def test_finalise_refused_cancels_pending_order_and_reverses_stock():
    biz = _biz(inventory_enabled=True)
    order_id = uuid.uuid4()
    session = Session(state=ConversationState.COLLECTING_PAYMENT.value,
                      ctx={"pending_order_id": str(order_id), "delivery_fee_cents": 2000,
                           "payment_method": "CASH_ON_COLLECTION", "delivery_fee_status": "APPROVED"})
    db = FakeDB([_result(scalar="FEE_SENT"), _result(scalar=order_id)])
    with refused(), \
         patch("backend.app.inventory.cancellation.reverse_consumption_on_cancel", AsyncMock()) as reverse, \
         patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()) as usage:
        text, *_ = await pipeline._finalize_pending_delivery_order(db, biz, MagicMock(), session)
    assert text.startswith(NOTICE_START)
    sqls = [str(c.args[0]) for c in db.execute.await_args_list]
    assert len(sqls) == 2 and sqls[1].startswith("UPDATE orders")
    cancel_stmt = db.execute.await_args_list[1].args[0]
    assert cancel_stmt.compile().params["status"] == "CANCELLED"
    events = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], OrderEvent)]
    assert [(e.old_status, e.new_status) for e in events] == [("FEE_SENT", "CANCELLED")]
    reverse.assert_awaited_once()
    assert reverse.await_args.kwargs["previous_status"] == "FEE_SENT"
    usage.assert_not_awaited()  # no order counted
    assert session.state == ConversationState.IDLE.value
    assert state_machine.get_context(session, "pending_order_id") is None


async def test_finalise_refused_never_cancels_an_order_already_past_pending():
    """A pending_order_id pointing at an order that is already NEW/ACCEPTED is left alone."""
    biz = _biz()
    session = Session(state=ConversationState.COLLECTING_PAYMENT.value,
                      ctx={"pending_order_id": str(uuid.uuid4())})
    db = FakeDB([_result(scalar="ACCEPTED")])
    with refused():
        await pipeline._finalize_pending_delivery_order(db, biz, MagicMock(), session)
    assert db.execute.await_count == 1  # status read only, no UPDATE
    db.add.assert_not_called()


async def test_online_payment_fee_yes_path_is_refused():
    biz = _biz(online_payment_required=True)
    session = Session(state=ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value,
                      ctx={"pending_order_id": str(uuid.uuid4()), "delivery_fee_cents": 2500})
    db = FakeDB([_result(scalar="FEE_SENT"), _result(scalar=uuid.uuid4())])
    with refused(), patch("backend.app.inventory.cancellation.reverse_consumption_on_cancel", AsyncMock()):
        text, *_ = await pipeline._handle_waiting_delivery_fee(db, biz, MagicMock(), session, "yes")
    assert text.startswith(NOTICE_START)
    assert not any("SET status=:status, payment_status" in str(c.args[0]) for c in db.execute.await_args_list)


# -- restaurants unaffected (gate functions made to raise) --------------------------

@contextlib.contextmanager
def gates_explode():
    boom = AsyncMock(side_effect=AssertionError("restaurant reached the event gate"))
    with patch.object(trading, "business_access_decision", boom), \
         patch.object(pipeline, "bot_event_context", boom), \
         patch.object(trading, "_require_trade_at", boom):
        yield


async def test_restaurant_pickup_confirmation_still_creates_order():
    biz = _biz(plan="STARTER")
    session = Session(ctx={"cart": _cart(), "order_mode": "PICKUP", "order_notes_asked": True})
    order = SimpleNamespace(id=uuid.uuid4(), order_number="BO-1", subtotal_cents=3000, delivery_fee_cents=0,
                            order_mode="PICKUP", total_cents=3000)
    db = FakeDB()
    with gates_explode(), \
         patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock(return_value=order)) as create, \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()), \
         patch.object(pipeline, "_cancel_prior_live_orders", AsyncMock()), \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()), \
         patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()):
        text, *_ = await pipeline._handle_order_confirmation(db, biz, MagicMock(), session)
    create.assert_awaited_once()
    assert "BO-1" in text
    db.execute.assert_not_awaited()  # the gate added zero queries
    assert session.state == ConversationState.ORDER_PLACED.value


async def test_restaurant_delivery_and_finalise_still_work():
    biz = _biz(plan="STARTER", delivery_enabled=True)
    session = Session(ctx={"cart": _cart(), "order_mode": "DELIVERY", "delivery_address": "1 Main Rd",
                           "order_notes_asked": True})
    order = SimpleNamespace(id=uuid.uuid4())
    with gates_explode(), \
         patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock(return_value=order)) as create, \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()), \
         patch.object(pipeline, "_cancel_prior_live_orders", AsyncMock()), \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()):
        await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)
    assert create.await_args.kwargs["initial_status"] == "PENDING_DELIVERY_FEE"
    assert session.state == ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value

    db = FakeDB([_result(one=SimpleNamespace(order_number="BO-2", delivery_fee_cents=2000, total_cents=5000))])
    fin = Session(state=ConversationState.COLLECTING_PAYMENT.value,
                  ctx={"pending_order_id": str(order.id), "payment_method": "CASH_ON_COLLECTION"})
    with gates_explode(), patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()), \
         patch("backend.app.db.session.get_redis", AsyncMock(side_effect=RuntimeError("no redis"))):
        text, *_ = await pipeline._finalize_pending_delivery_order(db, biz, MagicMock(), fin)
    assert "BO-2" in text and fin.state == ConversationState.ORDER_PLACED.value
    assert db.execute.await_count == 1  # only the finalise UPDATE


# -- soft gate ---------------------------------------------------------------------

def _soft_patches(creds=object(), refusal="NO_PAID_COVER_TODAY", session=None):
    session = session or Session(state=ConversationState.BUILDING_CART.value, ctx={"cart": _cart()})
    return session, [
        patch("backend.app.whatsapp.token_resolver.resolve_whatsapp_credentials", AsyncMock(return_value=creds)),
        patch.object(pipeline, "bot_trade_refusal", AsyncMock(return_value=refusal)),
        patch.object(pipeline, "bot_event_context", AsyncMock(return_value=_ctx())),
        patch.object(pipeline.state_machine, "get_or_create_session", AsyncMock(return_value=session)),
        patch.object(pipeline, "_send_response", AsyncMock()),
        patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()),
    ]


async def _soft(biz, **kw):
    session, ps = _soft_patches(**kw)
    with contextlib.ExitStack() as st:
        mocks = [st.enter_context(p) for p in ps]
        stop = await pipeline._event_vendor_soft_gate(FakeDB(), biz, MagicMock(id=uuid.uuid4()), "27820000000")
    return stop, session, mocks


async def test_soft_gate_wa_disabled_processes_nothing():
    stop, _, mocks = await _soft(_biz(is_whatsapp_enabled=False))
    assert stop is True
    mocks[0].assert_not_awaited()  # not even credentials
    mocks[4].assert_not_awaited()  # nothing sent


async def test_soft_gate_no_credentials_processes_nothing():
    stop, _, mocks = await _soft(_biz(), creds=None)
    assert stop is True
    mocks[1].assert_not_awaited()
    mocks[4].assert_not_awaited()


async def test_soft_gate_trade_day_lets_message_through():
    stop, session, mocks = await _soft(_biz(), refusal=None)
    assert stop is False
    mocks[4].assert_not_awaited()
    assert len(state_machine.get_cart(session)) == 1  # cart untouched


async def test_soft_gate_notice_once_per_event_local_day():
    biz = _biz()
    session = Session(state=ConversationState.BUILDING_CART.value, ctx={"cart": _cart()})
    sends = []
    for day in ("2026-11-20", "2026-11-20", "2026-11-21"):
        _, ps = _soft_patches(session=session)
        ps[2] = patch.object(pipeline, "bot_event_context", AsyncMock(return_value=_ctx(day=day)))
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in ps]
            assert await pipeline._event_vendor_soft_gate(FakeDB(), biz, MagicMock(id=uuid.uuid4()), "2782") is True
        sends.append(mocks[4].await_count)
        assert state_machine.get_cart(session) == []
        assert session.state == ConversationState.IDLE.value
    assert sends == [1, 0, 1]


async def test_process_runs_soft_gate_after_persist_and_before_limit_and_llm():
    """Drives _process: the inbound row is reserved and persisted, then the gate stops it."""
    biz = _biz()
    customer = SimpleNamespace(id=uuid.uuid4(), opted_out=False, wa_id="2782")
    order = []
    db = FakeDB([_result(scalar=None), _result(scalar=uuid.uuid4())])  # fast-path dedup, reserve insert
    biz_lookup = _result(scalar=biz)
    db.results.insert(0, biz_lookup)

    async def persist(**kw):
        order.append("persist")

    async def gate(*a, **k):
        order.append("gate")
        return True

    with patch.object(pipeline, "_get_or_create_customer", AsyncMock(return_value=customer)), \
         patch.object(pipeline, "_persist_inbound", AsyncMock(side_effect=persist)), \
         patch.object(pipeline, "_event_vendor_soft_gate", AsyncMock(side_effect=gate)), \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()) as limit, \
         patch.object(pipeline, "_handle_message", AsyncMock()) as handle:
        await pipeline._process(db=db, phone_number_id="pnid-1", wa_message_id="wamid.1", wa_id="2782",
                                msg_text="2 samoosas please", msg_type="text", raw_payload={},
                                contact_name=None)
    assert order == ["persist", "gate"]
    limit.assert_not_awaited()
    handle.assert_not_awaited()


async def test_process_restaurant_never_calls_soft_gate():
    biz = _biz(plan="STARTER")
    customer = SimpleNamespace(id=uuid.uuid4(), opted_out=False, wa_id="2782")
    db = FakeDB([_result(scalar=biz), _result(scalar=None), _result(scalar=uuid.uuid4())])
    with patch.object(pipeline, "_get_or_create_customer", AsyncMock(return_value=customer)), \
         patch.object(pipeline, "_persist_inbound", AsyncMock()), \
         patch.object(pipeline, "_event_vendor_soft_gate", AsyncMock(side_effect=AssertionError("gated"))), \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock(side_effect=pipeline.DailyLimitError("messages", 1))):
        await pipeline._process(db=db, phone_number_id="pnid-1", wa_message_id="wamid.2", wa_id="2782",
                                msg_text="hi", msg_type="text", raw_payload={}, contact_name=None)


@pytest.mark.parametrize("delivery", [False, True])
async def test_backstop_refusal_after_gate_passed_replies_instead_of_rolling_back(delivery):
    """Review L-2: gate (a) passes, then the trading day ends before order creation."""
    biz = _biz(delivery_enabled=delivery)
    ctx = {"cart": _cart(), "order_mode": "DELIVERY" if delivery else "PICKUP", "order_notes_asked": True}
    if delivery:
        ctx["delivery_address"] = "1 Main Rd"
    session = Session(ctx=ctx)
    refusal = AppError("EVENT_TRADING_NOT_ALLOWED", "no", 403)
    with patch.object(pipeline, "bot_trade_refusal", AsyncMock(return_value=None)), \
         patch.object(pipeline, "bot_event_context", AsyncMock(return_value=_ctx())), \
         patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock(side_effect=refusal)), \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()), \
         patch.object(pipeline, "_cancel_prior_live_orders", AsyncMock()) as prior, \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()), \
         patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()) as usage:
        text, *_ = await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)
    assert text.startswith(NOTICE_START)
    prior.assert_not_awaited()
    usage.assert_not_awaited()
    assert state_machine.get_cart(session) == [] and session.state == ConversationState.IDLE.value


async def test_other_app_errors_from_order_creation_still_propagate():
    biz = _biz()
    session = Session(ctx={"cart": _cart(), "order_mode": "PICKUP", "order_notes_asked": True})
    with patch.object(pipeline, "bot_trade_refusal", AsyncMock(return_value=None)), \
         patch.object(pipeline.order_creator, "create_order_from_cart", AsyncMock(side_effect=AppError("X", "y", 500))), \
         patch.object(pipeline, "_cancel_superseded_order", AsyncMock()), \
         patch.object(pipeline.usage_tracker, "check_daily_limit", AsyncMock()):
        with pytest.raises(AppError):
            await pipeline._handle_order_confirmation(FakeDB(), biz, MagicMock(), session)


async def test_backstop_check_failure_refuses_inside_a_savepoint():
    db = FakeDB()
    with patch.object(trading, "business_access_decision", AsyncMock(side_effect=RuntimeError("db"))):
        with pytest.raises(AppError) as exc:
            await trading.assert_bot_order_allowed(db, _biz())
    assert exc.value.details["reason"] == trading.BOT_ACCESS_CHECK_FAILED
    assert db.nested == 1


async def test_gate_cancellation_publishes_the_dashboard_event():
    """Review L-7: same order_status_changed event as a superseded cancellation."""
    import json
    biz = _biz()
    order_id = uuid.uuid4()
    session = Session(state=ConversationState.COLLECTING_PAYMENT.value, ctx={"pending_order_id": str(order_id)})
    db = FakeDB([_result(scalar="PENDING_DELIVERY_FEE"), _result(scalar=order_id)])
    redis = MagicMock()
    redis.publish = AsyncMock()
    with refused(), patch("backend.app.inventory.cancellation.reverse_consumption_on_cancel", AsyncMock()), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=redis)):
        await pipeline._finalize_pending_delivery_order(db, biz, MagicMock(), session)
    channel, payload = redis.publish.await_args.args
    assert channel == f"orders:{biz.id}"
    body = json.loads(payload)
    assert (body["type"], body["order_id"], body["old_status"], body["new_status"]) == (
        "order_status_changed", str(order_id), "PENDING_DELIVERY_FEE", "CANCELLED")


async def test_no_dashboard_event_when_nothing_was_cancelled():
    biz = _biz()
    session = Session(state=ConversationState.COLLECTING_PAYMENT.value, ctx={"pending_order_id": str(uuid.uuid4())})
    db = FakeDB([_result(scalar="ACCEPTED")])
    with refused(), patch("backend.app.db.session.get_redis", AsyncMock()) as get_redis:
        await pipeline._finalize_pending_delivery_order(db, biz, MagicMock(), session)
    get_redis.assert_not_awaited()
