"""
Real-Postgres concurrency proof for BUG-0157 (KNOWN_BUGS.md): a real,
reported production bug where a Mochachos WhatsApp customer's single
message ("Ok please give me the half chicken, chips and buddy.") produced
TWO separate "Added to your order" replies and a doubled cart (2x Half
Chicken, 2x Chips instead of 1x each).

Root cause, confirmed via a genuine concurrent-dispatch reproduction,
2026-10-26: Meta's webhook delivery is documented "at least once", not
exactly once, and the ARQ enqueue had no dedup key — so two concurrent
jobs for the same wa_message_id (backend/app/worker.py's
process_whatsapp_message creates its own DB session per job, exactly like
two real ARQ workers) could both pass pipeline.py's then-plain-SELECT
idempotency check before either committed. Two sub-bugs found this way:

1. _get_or_create_customer (pipeline.py): a brand-new customer's first
   message, delivered twice concurrently, crashed with an uncaught
   IntegrityError on uq_customers_business_wa_id -- the whole message was
   lost (no reply at all), not merely duplicated.
2. The wa_message_id idempotency check itself (step 4 of _process) was a
   TOCTOU race: a plain SELECT, with the actual uniqueness only enforced
   much later at final commit -- by which point cart mutations and the
   real WhatsApp send had already happened for both deliveries.

Fix: both now use INSERT ... ON CONFLICT DO NOTHING (same pattern this
file already used correctly for session-row creation), and
state_machine.get_or_create_session's SELECT now takes .with_for_update(),
serializing concurrent cart mutations for the same customer so the second
delivery sees the first's already-committed state instead of a stale
snapshot.

Follows the same style/precedent as test_loyalty_concurrency.py: each
concurrent attempt gets its OWN AsyncSession (an AsyncSession is not safe
for concurrent use from two coroutines at once), so the two genuinely race
against Postgres, not against each other's Python state in one process --
exactly how two real ARQ workers would race.
"""
import uuid
from unittest.mock import AsyncMock, patch

import asyncio
import pytest
from sqlalchemy import delete, select

from backend.app.bot import state_machine
from backend.app.bot.pipeline import process_inbound_message
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from tests.integration._db import get_session_factory


def _patches():
    return (
        patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message", new=AsyncMock(return_value="wamid.SENT")),
        patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)),
    )


async def _send(phone_number_id, wa_message_id, wa_id, text):
    async with get_session_factory()() as db:
        p1, p2 = _patches()
        with p1, p2:
            await process_inbound_message(
                db=db,
                phone_number_id=phone_number_id,
                wa_message_id=wa_message_id,
                wa_id=wa_id,
                msg_text=text,
                msg_type="text",
                raw_payload={"id": wa_message_id, "type": "text", "text": {"body": text}},
                contact_name="Concurrency Test",
            )


async def test_two_concurrent_deliveries_of_a_brand_new_customers_first_message_do_not_crash(
    test_business: Business, db,
):
    """
    Before the _get_or_create_customer fix: this crashed with an uncaught
    IntegrityError on uq_customers_business_wa_id on the first attempt,
    every time -- the message was entirely lost, not merely duplicated.
    """
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"concur-newcust-{unique}"
    test_business.is_active = True
    db.add(test_business)
    await db.commit()

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"
    wa_message_id = f"wamid.newcust-{uuid.uuid4().hex[:12]}"

    # Must not raise.
    await asyncio.gather(
        _send(test_business.whatsapp_phone_number_id, wa_message_id, wa_id, "Hello"),
        _send(test_business.whatsapp_phone_number_id, wa_message_id, wa_id, "Hello"),
    )

    async with get_session_factory()() as verify_db:
        customers = (
            await verify_db.execute(
                select(Customer).where(Customer.business_id == test_business.id, Customer.wa_id == wa_id)
            )
        ).scalars().all()
        assert len(customers) == 1, f"expected exactly one customer row, got {len(customers)}"

    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(Message).where(Message.business_id == test_business.id))
        await cleanup_db.execute(delete(ConversationSession).where(ConversationSession.business_id == test_business.id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == test_business.id))
        await cleanup_db.commit()


async def test_two_concurrent_deliveries_of_the_same_wa_message_id_mutate_the_cart_exactly_once(
    test_business: Business, db,
):
    """
    The actual reported symptom: an existing customer (already greeted,
    session already exists -- same shape as the real transcript's "Hello"
    /"menu" turns before the failing message), then ONE new wa_message_id
    delivered via TWO concurrent jobs. Before the fix, this could produce
    two "Added to your order" sends and a doubled cart. After the fix,
    exactly one Message row is ever committed for this wa_message_id, and
    the cart reflects exactly one application of the add.
    """
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"concur-cart-{unique}"
    test_business.is_active = True
    db.add(test_business)

    category = MenuCategory(business_id=test_business.id, name="Sides", sort_order=0)
    db.add(category)
    await db.flush()
    item = MenuItem(
        business_id=test_business.id, category_id=category.id,
        name="Chips", price_cents=3590, is_active=True,
    )
    db.add(item)
    await db.commit()

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"

    # Establish customer + session sequentially first (matches the real
    # transcript: "Hello" already happened before the failing message).
    await _send(test_business.whatsapp_phone_number_id, f"wamid.setup-{uuid.uuid4().hex[:12]}", wa_id, "Hello")

    race_wa_message_id = f"wamid.race-{uuid.uuid4().hex[:12]}"
    await asyncio.gather(
        _send(test_business.whatsapp_phone_number_id, race_wa_message_id, wa_id, "I'll have chips"),
        _send(test_business.whatsapp_phone_number_id, race_wa_message_id, wa_id, "I'll have chips"),
    )

    async with get_session_factory()() as verify_db:
        msgs = (
            await verify_db.execute(select(Message).where(Message.wa_message_id == race_wa_message_id))
        ).scalars().all()
        assert len(msgs) == 1, f"expected exactly one Message row for the race wa_message_id, got {len(msgs)}"

        customer = (
            await verify_db.execute(
                select(Customer).where(Customer.business_id == test_business.id, Customer.wa_id == wa_id)
            )
        ).scalar_one()
        session_row = (
            await verify_db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == customer.id)
            )
        ).scalar_one()
        cart = state_machine.get_cart(session_row)
        assert len(cart) == 1, f"expected exactly one cart line, got {cart}"
        assert cart[0]["name"] == "Chips"
        assert cart[0]["quantity"] == 1, (
            f"expected quantity 1 (not doubled by a duplicate delivery), got {cart[0]['quantity']}"
        )

    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(Message).where(Message.business_id == test_business.id))
        await cleanup_db.execute(delete(ConversationSession).where(ConversationSession.business_id == test_business.id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == test_business.id))
        await cleanup_db.execute(delete(MenuItem).where(MenuItem.business_id == test_business.id))
        await cleanup_db.execute(delete(MenuCategory).where(MenuCategory.business_id == test_business.id))
        await cleanup_db.commit()
