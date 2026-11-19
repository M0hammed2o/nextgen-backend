"""
H-3 (souk WhatsApp security review) — restaurant-affecting bug fix.

_finalize_pending_delivery_order used to UPDATE the order by id+business_id
only, so a delivery order already CANCELLED (45-minute fee timeout in the
worker, or staff) was resurrected to NEW by a late "yes"/"cash"; and it
rewrote total_cents = subtotal + fee, discarding the VAT/discount total that
set_delivery_fee (routes_orders.py) had stored.

These tests pin the fixed behaviour on mocks; the real-Postgres proof is in
tests/integration/test_event_whatsapp_gate_realdb.py (test_h3_*).
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.bot import pipeline, state_machine
from shared.enums import ConversationState
from shared.models.order import OrderEvent


class _Session:
    def __init__(self, ctx):
        self.id = uuid.uuid4()
        self.state = ConversationState.COLLECTING_PAYMENT.value
        self.context_json = dict(ctx)


def _business(**kw):
    b = SimpleNamespace(
        id=uuid.uuid4(), currency="ZAR", timezone="Africa/Johannesburg",
        online_payment_required=False, name="Test Restaurant",
    )
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _db_returning(row):
    db = MagicMock()
    result = MagicMock()
    result.one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture(autouse=True)
def _no_flag_modified():
    with patch("backend.app.bot.state_machine.flag_modified"):
        yield


@pytest.mark.asyncio
async def test_update_is_conditioned_on_pending_status_and_keeps_stored_totals():
    order_id = uuid.uuid4()
    session = _Session({"pending_order_id": str(order_id), "delivery_fee_cents": 3500,
                        "payment_method": "CASH_ON_COLLECTION"})
    # set_delivery_fee stored fee 3500 and a VAT-inclusive total of 14_375 on a 9_000 subtotal.
    db = _db_returning(SimpleNamespace(order_number="BO-0042", delivery_fee_cents=3500, total_cents=14_375))
    with patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()) as usage, \
         patch("backend.app.db.session.get_redis", AsyncMock(side_effect=RuntimeError("no redis"))):
        text, *_ = await pipeline._finalize_pending_delivery_order(db, _business(), MagicMock(), session)

    sql = _sql(db.execute.await_args.args[0])
    assert "orders.status IN ('PENDING_DELIVERY_FEE', 'FEE_SENT')" in sql
    assert "total_cents=" not in sql.split("RETURNING")[0]
    assert "delivery_fee_cents=" not in sql.split("RETURNING")[0]
    assert "R143.75" in text  # the stored VAT-correct total, not subtotal + fee
    assert usage.await_args.kwargs["revenue_cents"] == 14_375
    assert session.state == ConversationState.ORDER_PLACED.value


@pytest.mark.asyncio
async def test_cancelled_order_is_not_resurrected_customer_told_it_expired():
    order_id = uuid.uuid4()
    session = _Session({"pending_order_id": str(order_id), "delivery_fee_cents": 3500,
                        "payment_method": "CASH_ON_COLLECTION", "cart": [{"x": 1}]})
    db = _db_returning(None)  # UPDATE matched 0 rows: the order is no longer pending
    cancelled = MagicMock()
    cancelled.one_or_none.return_value = SimpleNamespace(status="CANCELLED", order_number="BO-0042")
    zero_rows = MagicMock()
    zero_rows.one_or_none.return_value = None
    db.execute = AsyncMock(side_effect=[zero_rows, cancelled])
    with patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()) as usage:
        text, *_ = await pipeline._finalize_pending_delivery_order(db, _business(), MagicMock(), session)

    assert "expired or was cancelled" in text
    assert "not been charged" in text
    usage.assert_not_awaited()
    assert not any(isinstance(c.args[0], OrderEvent) for c in db.add.call_args_list)
    assert session.state == ConversationState.IDLE.value
    assert state_machine.get_context(session, "pending_order_id") is None
    assert state_machine.get_cart(session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NEW", "ACCEPTED", "IN_PROGRESS", "READY"])
async def test_already_live_order_is_reported_as_placed_not_expired(status):
    """H-3 follow-up (review L-1): 0 rows can also mean the order is already live
    (staff moved FEE_SENT -> NEW, or a concurrent "yes" finalised it)."""
    session = _Session({"pending_order_id": str(uuid.uuid4()), "payment_method": "CASH_ON_COLLECTION",
                        "cart": [{"x": 1}]})
    zero_rows, live = MagicMock(), MagicMock()
    zero_rows.one_or_none.return_value = None
    live.one_or_none.return_value = SimpleNamespace(status=status, order_number="BO-0042")
    db = _db_returning(None)
    db.execute = AsyncMock(side_effect=[zero_rows, live])
    with patch.object(pipeline.usage_tracker, "increment_usage", AsyncMock()) as usage:
        text, *_ = await pipeline._finalize_pending_delivery_order(db, _business(), MagicMock(), session)
    assert "BO-0042" in text and "already been placed" in text
    assert "expired" not in text
    usage.assert_not_awaited()  # not counted twice
    assert not any(isinstance(c.args[0], OrderEvent) for c in db.add.call_args_list)
    assert session.state == ConversationState.ORDER_PLACED.value
    assert state_machine.get_cart(session) == []
