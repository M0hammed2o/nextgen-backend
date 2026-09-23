"""
Digital receipts — real-Postgres proofs for the properties a mock can't
honestly demonstrate: row-locked idempotency under genuine concurrency
(two separate DB connections racing against Postgres, not against each
other's Python state — same pattern as test_wave0a_pos_concurrency.py),
cross-tenant isolation, and the two real trigger points
(_mark_order_paid, update_order_status) wired end to end against real
Order/Business/Customer rows.

Requires TEST_DATABASE_URL to point at a reachable real Postgres migrated
to at least 0039_digital_receipts — set explicitly when invoking pytest
(see docker-compose.test.yml / TESTING.md). Fails loudly if TEST_DATABASE_URL
isn't set at all (via the `db`/`test_business` fixtures in
tests/integration/conftest.py, see tests/integration/_env.py). Every
business/order/customer row created here uses a fresh uuid4-suffixed
identifier so this file is safely re-runnable.

The actual Meta HTTP call is never made — every test patches
`backend.app.bot.whatsapp_sender.send_template_message` (the exact name
send_digital_receipt imports at call time), so this proves the DB-side
correctness (locking, idempotency, status transitions) without any network
dependency.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, func, select

from backend.app.api.v1.routes_orders import StatusUpdateRequest, update_order_status
from backend.app.api.v1.routes_payments import _mark_order_paid
from backend.app.api.v1.routes_receipts import get_digital_receipts_usage
from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.core.rbac import AuthUser
from tests.integration._db import get_session_factory
from backend.app.receipts.whatsapp_receipt import send_digital_receipt
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.digital_receipt import DigitalReceipt
from shared.models.order import Order
from shared.utils import generate_business_code


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _make_business(db, *, digital_receipts_enabled=True, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name="Digital Receipts Test Restaurant",
        slug=f"digital-receipts-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        digital_receipts_enabled=digital_receipts_enabled,
        whatsapp_phone_number_id=f"1{uuid.uuid4().int % 10**9:09d}",
        is_whatsapp_enabled=True,
    )
    defaults.update(overrides)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_customer(db, business: Business, **overrides) -> Customer:
    defaults = dict(business_id=business.id, wa_id="27821234567", phone_number=None)
    defaults.update(overrides)
    customer = Customer(**defaults)
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return customer


async def _make_order(db, business: Business, **overrides) -> Order:
    defaults = dict(
        business_id=business.id,
        order_number=f"BO-{uuid.uuid4().hex[:8].upper()}",
        status="NEW",
        payment_status="UNPAID",
        source="WHATSAPP",
        total_cents=9000,
        currency="ZAR",
        payment_required=True,
    )
    defaults.update(overrides)
    order = Order(**defaults)
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


async def _cleanup(db, business: Business):
    await db.execute(delete(DigitalReceipt).where(DigitalReceipt.business_id == business.id))
    await db.execute(delete(Order).where(Order.business_id == business.id))
    await db.execute(delete(Customer).where(Customer.business_id == business.id))
    await db.execute(delete(Business).where(Business.id == business.id))
    await db.commit()


@pytest.fixture
async def business(db):
    biz = await _make_business(db)
    yield biz
    await _cleanup(db, biz)


def _ok_send(wamid="wamid.TEST"):
    return AsyncMock(return_value=(wamid, None))


def _failed_send(retryable=True, message="transient failure"):
    info = MetaErrorInfo(
        status_code=500, code=None, subcode=None, message=message,
        details=None, retryable=retryable, last_error=f"TEST: {message}",
    )
    return AsyncMock(return_value=(None, info))


class TestIdempotentGetOrCreate:
    async def test_second_call_reuses_the_same_row(self, db, business):
        order = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()):
            first = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)
            second = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)

        assert first.id == second.id
        rows = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalars().all()
        assert len(rows) == 1


class TestConcurrentDuplicateRequests:
    async def test_two_simultaneous_requests_produce_exactly_one_row_and_one_send(self, business):
        async with get_session_factory()() as setup_db:
            order = await _make_order(setup_db, business)
            customer = await _make_customer(setup_db, business)
            order_id, customer_id = order.id, customer.id

        mock_send = _ok_send()

        async def attempt():
            async with get_session_factory()() as session:
                order_row = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
                customer_row = (
                    await session.execute(select(Customer).where(Customer.id == customer_id))
                ).scalar_one()
                receipt = await send_digital_receipt(
                    session, order_row, business, channels={"WHATSAPP"}, customer=customer_row,
                )
                return receipt.id

        with patch("backend.app.bot.whatsapp_sender.send_template_message", mock_send):
            results = await asyncio.gather(attempt(), attempt())

        assert results[0] == results[1], "both concurrent calls must resolve to the SAME receipt row"
        assert mock_send.call_count == 1, (
            f"expected exactly one WhatsApp send attempt under concurrency, got {mock_send.call_count}"
        )

        async with get_session_factory()() as check_db:
            rows = (
                await check_db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order_id))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].whatsapp_status == "SENT"


class TestFailedIsAlwaysRetryable:
    async def test_failed_send_can_be_retried_and_updates_the_same_row(self, db, business):
        order = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _failed_send()):
            first = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)
        assert first.whatsapp_status == "FAILED"
        assert first.whatsapp_failure_reason

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.RETRY")):
            second = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)

        assert second.id == first.id
        assert second.whatsapp_status == "SENT"
        assert second.whatsapp_wamid == "wamid.RETRY"


class TestAlreadySentIsNoopUnlessForceResend:
    async def test_repeat_call_without_force_resend_does_not_resend(self, db, business):
        order = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.FIRST")) as mock1:
            await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)
        assert mock1.call_count == 1

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.SECOND")) as mock2:
            repeat = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)

        assert mock2.call_count == 0
        assert repeat.whatsapp_wamid == "wamid.FIRST"

    async def test_force_resend_true_resends(self, db, business):
        order = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.FIRST")):
            await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.FORCED")) as mock2:
            forced = await send_digital_receipt(
                db, order, business, channels={"WHATSAPP"}, customer=customer, force_resend=True,
            )

        assert mock2.call_count == 1
        assert forced.whatsapp_wamid == "wamid.FORCED"


class TestEntitlementGatesWhatsappOnlyNeverPrint:
    async def test_whatsapp_disabled_still_records_print_request(self, db):
        biz = await _make_business(db, digital_receipts_enabled=False)
        try:
            order = await _make_order(db, biz)
            customer = await _make_customer(db, biz)

            with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
                receipt = await send_digital_receipt(
                    db, order, biz, channels={"PRINT", "WHATSAPP"}, customer=customer,
                )

            assert mock.call_count == 0
            assert receipt.print_requested is True
            assert receipt.whatsapp_status == "NOT_REQUESTED"
        finally:
            await _cleanup(db, biz)


class TestNoPhoneNumberAvailable:
    async def test_no_customer_no_phone_marks_failed_without_sending(self, db, business):
        order = await _make_order(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            receipt = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=None)

        assert mock.call_count == 0
        assert receipt.whatsapp_status == "FAILED"
        assert receipt.whatsapp_failure_reason == "no phone number available"


class TestCrossTenantIsolation:
    async def test_digital_receipt_rows_are_scoped_per_business(self, db, business):
        other = await _make_business(db)
        try:
            order_a = await _make_order(db, business)
            order_b = await _make_order(db, other)
            customer_a = await _make_customer(db, business)
            customer_b = await _make_customer(db, other)

            with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()):
                await send_digital_receipt(db, order_a, business, channels={"WHATSAPP"}, customer=customer_a)
                await send_digital_receipt(db, order_b, other, channels={"WHATSAPP"}, customer=customer_b)

            usage_a = await get_digital_receipts_usage(user=_owner(business.id), db=db)
            usage_b = await get_digital_receipts_usage(user=_owner(other.id), db=db)

            assert usage_a["receipts_requested"] == 1
            assert usage_b["receipts_requested"] == 1

            # A business-A-scoped query must never see business B's token.
            token_a = (
                await db.execute(select(DigitalReceipt.token).where(DigitalReceipt.business_id == business.id))
            ).scalar_one()
            token_b = (
                await db.execute(select(DigitalReceipt.token).where(DigitalReceipt.business_id == other.id))
            ).scalar_one()
            assert token_a != token_b
            business_a_tokens = (
                await db.execute(select(DigitalReceipt.token).where(DigitalReceipt.business_id == business.id))
            ).scalars().all()
            assert token_b not in business_a_tokens
        finally:
            await _cleanup(db, other)


class TestUsageEndpointCounts:
    async def test_totals_match_real_counts(self, db, business):
        order1 = await _make_order(db, business)
        order2 = await _make_order(db, business)
        order3 = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()):
            await send_digital_receipt(db, order1, business, channels={"WHATSAPP"}, customer=customer)
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _failed_send()):
            await send_digital_receipt(db, order2, business, channels={"WHATSAPP"}, customer=customer)
        await send_digital_receipt(db, order3, business, channels={"PRINT"}, customer=customer)

        usage = await get_digital_receipts_usage(user=_owner(business.id), db=db)
        assert usage["receipts_requested"] == 3
        assert usage["whatsapp_sent"] == 1
        assert usage["whatsapp_failed"] == 1


class TestDisablingEntitlementDoesNotAffectExistingTokens:
    async def test_existing_sent_receipt_row_is_untouched_by_disabling_later(self, db, business):
        order = await _make_order(db, business)
        customer = await _make_customer(db, business)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.KEEP")):
            receipt = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)
        token = receipt.token
        assert receipt.whatsapp_status == "SENT"

        business.digital_receipts_enabled = False
        await db.commit()

        refetched = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.token == token))
        ).scalar_one()
        assert refetched.whatsapp_status == "SENT"
        assert refetched.whatsapp_wamid == "wamid.KEEP"


class TestCashOnCollectionTrigger:
    async def test_receipt_sent_exactly_once_on_genuine_collected_transition(self, db, business):
        customer = await _make_customer(db, business)
        order = await _make_order(
            db, business,
            status="READY", payment_status="CASH_ON_COLLECTION",
            payment_required=False, customer_id=customer.id,
        )
        user = _staff(business.id)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            await update_order_status(
                order.id, StatusUpdateRequest(status="COLLECTED"), user=user, db=db,
            )

        assert mock.call_count == 1
        receipt = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalar_one()
        assert receipt.whatsapp_status == "SENT"

        # A repeat status update attempt on an already-terminal order is
        # rejected by the state machine itself (COLLECTED has no outbound
        # transitions) before the receipt trigger is ever reached again.
        from backend.app.core.errors import InvalidTransitionError

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock2:
            with pytest.raises(InvalidTransitionError):
                await update_order_status(
                    order.id, StatusUpdateRequest(status="COLLECTED"), user=user, db=db,
                )
        assert mock2.call_count == 0

    async def test_not_triggered_for_a_paid_order_transitioning_to_collected(self, db, business):
        """An order paid via a payment provider (payment_status=PAID, not
        CASH_ON_COLLECTION) must not fire this trigger — that path is
        exclusively _mark_order_paid's own webhook-driven send."""
        customer = await _make_customer(db, business)
        order = await _make_order(
            db, business,
            status="READY", payment_status="PAID", payment_required=True, customer_id=customer.id,
        )
        user = _staff(business.id)

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            await update_order_status(order.id, StatusUpdateRequest(status="COLLECTED"), user=user, db=db)

        assert mock.call_count == 0
        rows = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalars().all()
        assert rows == []


class TestProviderWebhookPaidTrigger:
    async def test_receipt_sent_exactly_once_on_paid_not_again_on_duplicate_webhook(self, db, business):
        customer = await _make_customer(db, business)
        order = await _make_order(
            db, business, status="NEW", payment_status="UNPAID",
            payment_required=True, customer_id=customer.id,
        )

        with patch("backend.app.bot.whatsapp_sender.send_notification_message", new_callable=AsyncMock), \
             patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            await _mark_order_paid(db, str(order.id), business.id, "TestProvider", "ref-1", amount_cents=9000, currency="ZAR")
            # Duplicate delivery of the same webhook — order already PAID.
            await _mark_order_paid(db, str(order.id), business.id, "TestProvider", "ref-1", amount_cents=9000, currency="ZAR")

        assert mock.call_count == 1
        rows = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].whatsapp_status == "SENT"
