"""
BUG-0073 — a resend with no phone available must never downgrade an
already-SENT receipt.

Real-Postgres proofs, because the defect is about what is DURABLY written
to (and NOT written to) the digital_receipts row: a mock session cannot
honestly show that a rolled-back resend left status/wamid/sent_at
byte-for-byte unchanged. Skips cleanly if DATABASE_URL is unreachable,
matching tests/test_whatsapp_connections_realdb.py:

    ENVIRONMENT=test
    DATABASE_URL=postgresql+asyncpg://nextgen:...@localhost:55444/nextgen_test_gatefix
    python -m pytest tests/test_receipt_resend_no_downgrade_realdb.py -q

No network call is made anywhere — send_template_message is patched at the
exact name send_digital_receipt imports at call time.

The scenario is the one the gate pass actually hit: a WALK-IN POS sale, so
there is no linked Customer and no Order.phone_number. The only record of
who the receipt went to is DigitalReceipt.phone_number_e164 itself, which
is precisely what resolution used to ignore.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select, text

from backend.app.api.v1.routes_orders import ReceiptResendRequest, resend_order_receipt
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine
from backend.app.receipts.whatsapp_receipt import (
    ReceiptDestinationUnavailable,
    _resolve_whatsapp_destination,
    send_digital_receipt,
)
from shared.models.business import Business
from shared.models.digital_receipt import DigitalReceipt
from shared.models.order import Order
from shared.utils import generate_business_code

SENT_PHONE = "+27821234567"
SENT_WA_ID = "27821234567"


# ── Pure unit-level proof of the resolution-priority half ──────────────────
# (no DB needed — these run in the default suite too)

class TestStoredNumberFallback:
    def test_stored_number_used_when_nothing_else_resolves(self):
        wa_id, persist = _resolve_whatsapp_destination(
            phone_number_e164=None, customer=None, stored_phone_e164=SENT_PHONE,
        )
        assert wa_id == SENT_WA_ID
        # Already on the row — nothing new to write back.
        assert persist is None

    def test_explicit_phone_still_beats_the_stored_number(self):
        """Correcting a mistyped number must keep working."""
        wa_id, persist = _resolve_whatsapp_destination(
            phone_number_e164="+27835550000", customer=None, stored_phone_e164=SENT_PHONE,
        )
        assert wa_id == "27835550000"
        assert persist == "+27835550000"

    def test_linked_customer_still_beats_the_stored_number(self):
        customer = types.SimpleNamespace(id=uuid.uuid4(), wa_id="27849998888", phone_number=None)
        wa_id, _ = _resolve_whatsapp_destination(
            phone_number_e164=None, customer=customer, stored_phone_e164=SENT_PHONE,
        )
        assert wa_id == "27849998888"

    def test_nothing_at_all_still_resolves_to_nothing(self):
        wa_id, persist = _resolve_whatsapp_destination(
            phone_number_e164=None, customer=None, stored_phone_e164=None,
        )
        assert wa_id is None
        assert persist is None


# ── Real-DB fixtures ────────────────────────────────────────────────────────

async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db():
    if not await _db_reachable():
        pytest.skip("Real Postgres not reachable at DATABASE_URL — see file docstring.")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


async def _make_business(db) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Resend Test {unique}",
        slug=f"resend-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        digital_receipts_enabled=True,
        is_whatsapp_enabled=True,
        whatsapp_phone_number_id=f"1{uuid.uuid4().int % 10**9:09d}",
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_walkin_order(db, business) -> Order:
    """A POS cash sale: no customer_id, no phone_number — the BUG-0073 shape."""
    order = Order(
        business_id=business.id,
        order_number=f"R-{uuid.uuid4().hex[:8].upper()}",
        status="COMPLETED",
        payment_status="PAID",
        source="POS",
        customer_id=None,
        phone_number=None,
        total_cents=10000,
        currency="ZAR",
        payment_required=False,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


async def _cleanup(db, business):
    await db.execute(delete(DigitalReceipt).where(DigitalReceipt.business_id == business.id))
    await db.execute(delete(Order).where(Order.business_id == business.id))
    await db.execute(delete(Business).where(Business.id == business.id))
    await db.commit()


def _ok_send(wamid="wamid.ORIGINAL"):
    return AsyncMock(return_value=(wamid, None))


async def _original_send(db, order, business, wamid="wamid.ORIGINAL"):
    """The original, successful send — a staff-typed number on a walk-in sale."""
    with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send(wamid)):
        receipt = await send_digital_receipt(
            db, order, business,
            channels={"WHATSAPP"},
            phone_number_e164=SENT_PHONE,
            customer=None,
        )
    assert receipt.whatsapp_status == "SENT"
    assert receipt.phone_number_e164 == SENT_PHONE
    return receipt


# ── Half 1: the resend must reuse the row's own stored number ──────────────

class TestResendFallsBackToStoredNumber:
    async def test_dashboard_resend_with_empty_body_reuses_stored_number(self, db):
        """
        The exact gate repro: manager dashboard Resend posts an empty body.
        Before the fix this resolved to no destination and wrote FAILED over
        the SENT row; it must now re-send to the stored number.
        """
        business = await _make_business(db)
        try:
            order = await _make_walkin_order(db, business)
            await _original_send(db, order, business)

            resend_mock = _ok_send("wamid.RESENT")
            with patch("backend.app.bot.whatsapp_sender.send_template_message", resend_mock):
                response = await resend_order_receipt(
                    order_id=order.id,
                    body=ReceiptResendRequest(),  # no phone_number — the bug trigger
                    user=AuthUser(
                        user_id=uuid.uuid4(), business_id=business.id,
                        role="MANAGER", token_type="access",
                    ),
                    db=db,
                )

            # A real second send actually happened, to the stored number.
            assert resend_mock.call_count == 1
            assert resend_mock.call_args.kwargs["recipient_wa_id"] == SENT_WA_ID

            assert response.whatsapp_status == "SENT"
            assert response.whatsapp_failure_reason is None

            row = (
                await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
            ).scalar_one()
            assert row.whatsapp_status == "SENT"
            assert row.whatsapp_wamid == "wamid.RESENT"
            assert row.phone_number_e164 == SENT_PHONE
        finally:
            await _cleanup(db, business)

    async def test_pos_force_resend_without_phone_reuses_stored_number(self, db):
        """Same fallback via the POS force_resend path, not just the dashboard."""
        business = await _make_business(db)
        try:
            order = await _make_walkin_order(db, business)
            await _original_send(db, order, business)

            resend_mock = _ok_send("wamid.POSRESENT")
            with patch("backend.app.bot.whatsapp_sender.send_template_message", resend_mock):
                receipt = await send_digital_receipt(
                    db, order, business,
                    channels={"WHATSAPP"},
                    phone_number_e164=None,
                    customer=None,
                    force_resend=True,
                )

            assert resend_mock.call_count == 1
            assert receipt.whatsapp_status == "SENT"
            assert receipt.whatsapp_wamid == "wamid.POSRESENT"
        finally:
            await _cleanup(db, business)


# ── Half 2: the independent no-downgrade safeguard ─────────────────────────

_SNAPSHOT_COLS = (
    DigitalReceipt.whatsapp_status,
    DigitalReceipt.whatsapp_wamid,
    DigitalReceipt.whatsapp_sent_at,
    DigitalReceipt.whatsapp_failure_reason,
    DigitalReceipt.phone_number_e164,
)


async def _snapshot(db, order_id):
    return (
        await db.execute(select(*_SNAPSHOT_COLS).where(DigitalReceipt.order_id == order_id))
    ).one()


class TestSentRowIsNeverDowngraded:
    async def test_unresolvable_resend_leaves_sent_row_byte_for_byte_unchanged(self, db):
        """
        Construct the case where even the stored phone is absent (shouldn't
        happen in practice — proving the second, independent safeguard).
        The SENT row must survive completely untouched and the caller must
        get a clear error, not a silent success.
        """
        business = await _make_business(db)
        try:
            order = await _make_walkin_order(db, business)
            receipt = await _original_send(db, order, business)

            # Erase the stored number so NOTHING is resolvable.
            receipt.phone_number_e164 = None
            await db.commit()
            await db.refresh(receipt)

            before = await _snapshot(db, order.id)

            send_mock = _ok_send("wamid.SHOULD-NEVER-HAPPEN")
            with patch("backend.app.bot.whatsapp_sender.send_template_message", send_mock):
                with pytest.raises(ReceiptDestinationUnavailable):
                    await send_digital_receipt(
                        db, order, business,
                        channels={"WHATSAPP"},
                        phone_number_e164=None,
                        customer=None,
                        force_resend=True,
                    )

            # No send was attempted, so no delivery claim changed.
            assert send_mock.call_count == 0

            after = await _snapshot(db, order.id)
            assert after == before, "a failed resend mutated an already-SENT receipt row"
            assert after.whatsapp_status == "SENT"
        finally:
            await _cleanup(db, business)

    async def test_dashboard_resend_surfaces_422_not_a_silent_success(self, db):
        """The route must report the caller error, not a fake delivery failure."""
        business = await _make_business(db)
        try:
            order = await _make_walkin_order(db, business)
            receipt = await _original_send(db, order, business)
            receipt.phone_number_e164 = None
            await db.commit()

            with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()):
                with pytest.raises(AppError) as exc_info:
                    await resend_order_receipt(
                        order_id=order.id,
                        body=ReceiptResendRequest(),
                        user=AuthUser(
                            user_id=uuid.uuid4(), business_id=business.id,
                            role="MANAGER", token_type="access",
                        ),
                        db=db,
                    )

            assert exc_info.value.status_code == 422
            assert exc_info.value.code == "RECEIPT_NO_DESTINATION"

            row = (
                await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
            ).scalar_one()
            assert row.whatsapp_status == "SENT"
            assert row.whatsapp_failure_reason is None
        finally:
            await _cleanup(db, business)


# ── A not-yet-delivered receipt must STILL be markable FAILED ──────────────

class TestUndeliveredReceiptStillMarksFailed:
    async def test_first_send_with_no_destination_still_marks_failed(self, db):
        """
        The safeguard is scoped to SENT/DELIVERED only — the pre-existing
        "no phone number available" behaviour for a receipt that was never
        delivered must be unchanged (see tests/integration/
        test_digital_receipts_realdb.py::TestNoPhoneNumberAvailable).
        """
        business = await _make_business(db)
        try:
            order = await _make_walkin_order(db, business)
            with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
                receipt = await send_digital_receipt(
                    db, order, business, channels={"WHATSAPP"}, customer=None,
                )
            assert mock.call_count == 0
            assert receipt.whatsapp_status == "FAILED"
            assert receipt.whatsapp_failure_reason == "no phone number available"
        finally:
            await _cleanup(db, business)
