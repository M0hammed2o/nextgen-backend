"""
Staff/manager-facing digital-receipt request endpoints — real-Postgres.

    POST /business/pos/orders/{order_id}/receipt        (routes_pos.py)
    POST /business/orders/{order_id}/receipt/resend      (routes_orders.py)

Called directly (route functions, not via TestClient/HTTP), the same
pattern every other real-DB integration test in this directory uses —
RBAC dependency injection itself (require_staff_or_above/require_owner_or_
manager as FastAPI Depends) is exercised by this codebase's existing RBAC
test coverage; these tests instead prove the IN-FUNCTION business logic:
tenant scoping via require_owned, the explicit force_resend role check,
phone-number validation, and end-to-end wiring against a real
send_digital_receipt/DigitalReceipt row.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from backend.app.api.v1.routes_orders import ReceiptResendRequest, resend_order_receipt
from backend.app.api.v1.routes_pos import POSReceiptRequest, request_pos_receipt
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.digital_receipt import DigitalReceipt
from shared.models.order import Order
from shared.utils import generate_business_code


def _staff(business_id) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


def _owner(business_id) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _make_business(db, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name="Receipt Endpoint Test Restaurant",
        slug=f"receipt-endpoint-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        digital_receipts_enabled=True,
        whatsapp_phone_number_id=f"1{uuid.uuid4().int % 10**9:09d}",
        is_whatsapp_enabled=True,
    )
    defaults.update(overrides)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_order(db, business, **overrides) -> Order:
    defaults = dict(
        business_id=business.id,
        order_number=f"BO-{uuid.uuid4().hex[:8].upper()}",
        status="COMPLETED",
        payment_status="PAID",
        source="POS",
        total_cents=9000,
        currency="ZAR",
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


class TestPOSReceiptEndpoint:
    async def test_staff_can_request_print_only(self, db, business):
        order = await _make_order(db, business)
        response = await request_pos_receipt(
            order.id, POSReceiptRequest(channels=["PRINT"]), user=_staff(business.id), db=db,
        )
        assert response.print_requested is True
        assert response.whatsapp_status == "NOT_REQUESTED"

    async def test_staff_cannot_force_resend(self, db, business):
        order = await _make_order(db, business)
        with pytest.raises(AppError) as exc_info:
            await request_pos_receipt(
                order.id,
                POSReceiptRequest(channels=["PRINT"], force_resend=True),
                user=_staff(business.id),
                db=db,
            )
        assert exc_info.value.code == "FORBIDDEN"

    async def test_owner_can_force_resend_after_initial_send(self, db, business):
        order = await _make_order(db, business)
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.FIRST")):
            first = await request_pos_receipt(
                order.id,
                POSReceiptRequest(channels=["WHATSAPP"], phone_number="0821234567"),
                user=_owner(business.id),
                db=db,
            )
        assert first.whatsapp_status == "SENT"

        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.FORCED")) as mock:
            forced = await request_pos_receipt(
                order.id,
                POSReceiptRequest(channels=["WHATSAPP"], phone_number="0821234567", force_resend=True),
                user=_owner(business.id),
                db=db,
            )
        assert mock.call_count == 1
        assert forced.id == first.id

    async def test_invalid_phone_number_is_rejected(self, db, business):
        order = await _make_order(db, business)
        with pytest.raises(AppError) as exc_info:
            await request_pos_receipt(
                order.id,
                POSReceiptRequest(channels=["WHATSAPP"], phone_number="123"),
                user=_staff(business.id),
                db=db,
            )
        assert exc_info.value.code == "INVALID_PHONE_NUMBER"

    async def test_valid_phone_number_is_normalised_and_used(self, db, business):
        order = await _make_order(db, business)
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            response = await request_pos_receipt(
                order.id,
                POSReceiptRequest(channels=["WHATSAPP"], phone_number="082 123 4567"),
                user=_staff(business.id),
                db=db,
            )
        assert response.whatsapp_status == "SENT"
        mock.assert_awaited_once()
        assert mock.call_args.kwargs["recipient_wa_id"] == "27821234567"

        receipt = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalar_one()
        assert receipt.phone_number_e164 == "+27821234567"

    async def test_cross_tenant_order_id_is_not_found(self, db, business):
        other = await _make_business(db)
        try:
            order = await _make_order(db, other)
            with pytest.raises(NotFoundError):
                await request_pos_receipt(
                    order.id, POSReceiptRequest(channels=["PRINT"]), user=_staff(business.id), db=db,
                )
        finally:
            await _cleanup(db, other)

    async def test_receipt_url_contains_the_token(self, db, business):
        order = await _make_order(db, business)
        response = await request_pos_receipt(
            order.id, POSReceiptRequest(channels=["PRINT"]), user=_staff(business.id), db=db,
        )
        receipt = (
            await db.execute(select(DigitalReceipt).where(DigitalReceipt.order_id == order.id))
        ).scalar_one()
        assert receipt.token in response.receipt_url
        assert response.receipt_url.endswith(f"/v1/r/{receipt.token}")


class TestManagerResendEndpoint:
    async def test_resend_works_for_a_manual_order_with_explicit_phone(self, db, business):
        order = await _make_order(db, business, source="MANUAL")
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send()) as mock:
            response = await resend_order_receipt(
                order.id,
                ReceiptResendRequest(phone_number="0821234567"),
                user=_owner(business.id),
                db=db,
            )
        assert response.whatsapp_status == "SENT"
        mock.assert_awaited_once()

    async def test_resend_forces_a_second_send_even_if_already_sent(self, db, business):
        order = await _make_order(db, business, source="MANUAL")
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.ONE")):
            await resend_order_receipt(
                order.id, ReceiptResendRequest(phone_number="0821234567"), user=_owner(business.id), db=db,
            )
        with patch("backend.app.bot.whatsapp_sender.send_template_message", _ok_send("wamid.TWO")) as mock:
            second = await resend_order_receipt(
                order.id, ReceiptResendRequest(phone_number="0821234567"), user=_owner(business.id), db=db,
            )
        assert mock.call_count == 1
        assert second.whatsapp_status == "SENT"

    async def test_invalid_phone_number_rejected(self, db, business):
        order = await _make_order(db, business, source="MANUAL")
        with pytest.raises(AppError) as exc_info:
            await resend_order_receipt(
                order.id, ReceiptResendRequest(phone_number="not a phone"), user=_owner(business.id), db=db,
            )
        assert exc_info.value.code == "INVALID_PHONE_NUMBER"
