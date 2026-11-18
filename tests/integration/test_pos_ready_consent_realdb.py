"""
Real-Postgres tests for the POS order-ready WhatsApp consent (migration 0065).
No real message is ever sent: ``send_notification_message`` is patched in every
test that reaches a send path.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from backend.app.api.v1.routes_orders import StatusUpdateRequest, update_order_status
from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
from shared.models.customer import Customer
from shared.models.order import Order
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    _order,
    _plain_business,
    _till_and_item,
    _user,
    bag,
)

pytestmark = pytest.mark.asyncio


async def test_notify_when_ready_defaults_false_and_requires_a_phone():
    base = dict(items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)], payment_method="CARD")
    assert POSOrderCreateRequest(**base).notify_when_ready is False
    with pytest.raises(ValidationError):
        POSOrderCreateRequest(**base, notify_when_ready=True)
    assert POSOrderCreateRequest(**base, customer_phone="0821234567", notify_when_ready=True).notify_when_ready


async def test_pos_order_persists_the_consent_flag(db, bag):
    biz = await _plain_business(db, bag, pos_enabled=True)
    owner = _user(biz.id)
    item, _ = await _till_and_item(biz.id, owner)
    flags = {}
    for consent in (False, True):
        out = await create_pos_order(
            POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                                  payment_method="CASH", cash_tendered_cents=5000,
                                  customer_phone="0821112222", notify_when_ready=consent), owner, db)
        flags[consent] = (await db.execute(select(Order.notify_when_ready).where(Order.id == out.id))).scalar_one()
    assert flags == {False: False, True: True}


async def _ready_setup(db, bag, *, source, notify, wa_id):
    biz = await _plain_business(db, bag, whatsapp_phone_number_id=f"pn{uuid.uuid4().hex[:12]}")
    customer = Customer(business_id=biz.id, wa_id=wa_id, phone_number="+27821112222")
    db.add(customer)
    await db.commit()
    order = await _order(db, bag, biz.id, status="IN_PROGRESS", source=source, notify_when_ready=notify,
                         customer_id=customer.id, phone_number="0821112222", payment_status="PAID")
    return biz, order


async def test_pos_ready_message_is_sent_only_with_consent(db, bag):
    with patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock(return_value="wamid")) as send:
        biz, no_consent = await _ready_setup(db, bag, source="POS", notify=False, wa_id="27821112222")
        await update_order_status(no_consent.id, StatusUpdateRequest(status="READY"), _user(biz.id, "STAFF"), db)
        assert send.await_count == 0  # a linked loyalty customer is NOT consent

        biz2, consent = await _ready_setup(db, bag, source="POS", notify=True, wa_id="27821112222")
        await update_order_status(consent.id, StatusUpdateRequest(status="READY"), _user(biz2.id, "STAFF"), db)
        assert send.await_count == 1
        assert send.await_args.kwargs["recipient_wa_id"] == "27821112222"
        assert "ready" in send.await_args.kwargs["text"].lower()


async def test_pos_consent_covers_ready_only_and_walk_up_phone_without_a_wa_id(db, bag):
    with patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock(return_value="wamid")) as send:
        biz, order = await _ready_setup(db, bag, source="POS", notify=True, wa_id=None)
        await update_order_status(order.id, StatusUpdateRequest(status="READY"), _user(biz.id, "STAFF"), db)
        assert send.await_count == 1
        assert send.await_args.kwargs["recipient_wa_id"] == "27821112222"  # normalised, no '+'
        await update_order_status(order.id, StatusUpdateRequest(status="COLLECTED"), _user(biz.id, "STAFF"), db)
        assert send.await_count == 1  # no further message to a walk-up customer


async def test_whatsapp_sourced_orders_keep_notifying_without_the_flag(db, bag):
    with patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock(return_value="wamid")) as send:
        biz, order = await _ready_setup(db, bag, source="WHATSAPP", notify=False, wa_id="27821112222")
        await update_order_status(order.id, StatusUpdateRequest(status="READY"), _user(biz.id, "STAFF"), db)
        assert send.await_count == 1


async def test_0065_column_exists_not_null_default_false(db):
    row = (await db.execute(text(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name='orders' AND column_name='notify_when_ready'"))).one()
    assert row[0] == "NO" and "false" in row[1]
