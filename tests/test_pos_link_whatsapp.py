"""
backend/app/payments/pos_link_whatsapp.py::send_pos_payment_link and
backend/app/payments/messages.py::build_pos_payment_request_message.

Mirrors tests/test_whatsapp_receipt.py / tests/test_whatsapp_sender_template.py's
conventions: no real DB, no real Meta HTTP call — every test patches
`backend.app.bot.whatsapp_sender.send_template_message` (the exact name
send_pos_payment_link imports at call time, same late-import pattern as
backend/app/receipts/whatsapp_receipt.py::send_digital_receipt).
"""

import logging
import types
import uuid
from unittest.mock import AsyncMock, patch

from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.payments.messages import build_pos_payment_request_message
from backend.app.payments.pos_link_whatsapp import send_pos_payment_link


def _intent(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        amount_cents=15000,
        currency="ZAR",
        customer_phone="+27821234567",
        whatsapp_status="NOT_SENT",
        whatsapp_message_id=None,
        whatsapp_failure_reason=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _business(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        name="Ciao Pizza",
        currency="ZAR",
        whatsapp_phone_number_id="1234567890",
        whatsapp_payment_link_template_name=None,
        whatsapp_payment_link_template_language=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class _FakeDB:
    """Stand-in session — send_pos_payment_link never queries the DB itself,
    it only passes `db` through to the (patched) send_template_message, and
    must never call commit()."""

    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


class TestBuildPosPaymentRequestMessage:
    def test_message_never_contains_raw_intent_uuid(self):
        intent = _intent()
        business = _business()
        message = build_pos_payment_request_message(intent, business, "https://pay.example/abc123")
        assert str(intent.id) not in message

    def test_message_contains_amount_and_link(self):
        intent = _intent(amount_cents=25050, currency="ZAR")
        business = _business(name="Ciao Pizza")
        message = build_pos_payment_request_message(intent, business, "https://pay.example/xyz")
        assert "Ciao Pizza" in message
        assert "R250.50" in message
        assert "https://pay.example/xyz" in message


class TestSendPosPaymentLinkSuccess:
    async def test_successful_send_sets_sent_and_message_id(self):
        intent = _intent()
        business = _business()
        db = _FakeDB()
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = ("wamid.pos123", None)
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "SENT"
        assert intent.whatsapp_message_id == "wamid.pos123"
        assert intent.whatsapp_failure_reason is None
        mock_send.assert_awaited_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["phone_number_id"] == business.whatsapp_phone_number_id
        assert kwargs["recipient_wa_id"] == "27821234567"
        assert kwargs["template_name"] == "nextgen_pos_payment_request"
        assert kwargs["language_code"] == "en_US"
        assert kwargs["db"] is db
        assert kwargs["business"] is business
        # Never commits — the caller owns the transaction.
        assert db.committed is False

    async def test_per_business_template_override_used(self):
        intent = _intent()
        business = _business(
            whatsapp_payment_link_template_name="custom_template",
            whatsapp_payment_link_template_language="af",
        )
        db = _FakeDB()
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = ("wamid.custom", None)
            await send_pos_payment_link(db, intent, business, "https://pay.example/custom")

        kwargs = mock_send.call_args.kwargs
        assert kwargs["template_name"] == "custom_template"
        assert kwargs["language_code"] == "af"


class TestSendPosPaymentLinkWabaNotConfigured:
    async def test_no_phone_number_id_sets_failed_and_never_raises(self):
        intent = _intent()
        business = _business(whatsapp_phone_number_id=None)
        db = _FakeDB()
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "FAILED"
        assert intent.whatsapp_failure_reason
        assert "whatsapp_phone_number_id" in intent.whatsapp_failure_reason
        mock_send.assert_not_awaited()

    async def test_no_customer_phone_sets_failed_and_never_raises(self):
        intent = _intent(customer_phone=None)
        business = _business()
        db = _FakeDB()
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "FAILED"
        assert intent.whatsapp_failure_reason
        mock_send.assert_not_awaited()


class TestSendPosPaymentLinkTemplateRequired:
    async def test_template_not_found_error_classifies_to_template_required(self):
        intent = _intent()
        business = _business()
        db = _FakeDB()
        error_info = MetaErrorInfo(
            status_code=400,
            code=132001,
            subcode=None,
            message="Template name does not exist in the translation",
            details=None,
            retryable=False,
            last_error="META_132001: Template name does not exist in the translation",
        )
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (None, error_info)
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "TEMPLATE_REQUIRED"
        assert intent.whatsapp_failure_reason
        assert intent.whatsapp_message_id is None

    async def test_template_error_detected_via_message_text_fallback(self):
        intent = _intent()
        business = _business()
        db = _FakeDB()
        error_info = MetaErrorInfo(
            status_code=400,
            code=999,
            subcode=None,
            message="Param template foo is not approved for this account",
            details=None,
            retryable=False,
            last_error="META_999: Param template foo is not approved for this account",
        )
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (None, error_info)
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "TEMPLATE_REQUIRED"


class TestSendPosPaymentLinkGenericFailure:
    async def test_generic_meta_error_classifies_to_failed_not_template_required(self):
        intent = _intent()
        business = _business()
        db = _FakeDB()
        error_info = MetaErrorInfo(
            status_code=470,
            code=131047,
            subcode=None,
            message="outside window",
            details=None,
            retryable=False,
            last_error="META_131047: outside window",
        )
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (None, error_info)
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "FAILED"
        assert intent.whatsapp_failure_reason == "META_131047: outside window"

    async def test_no_error_info_at_all_still_sets_failed_never_raises(self):
        intent = _intent()
        business = _business()
        db = _FakeDB()
        with patch(
            "backend.app.bot.whatsapp_sender.send_template_message",
            new_callable=AsyncMock,
        ) as mock_send:
            mock_send.return_value = (None, None)
            await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        assert intent.whatsapp_status == "FAILED"
        assert intent.whatsapp_failure_reason == "unknown error"


class TestPhoneNumberNeverLoggedUnmasked:
    async def test_full_phone_number_never_appears_in_log_output(self, caplog):
        intent = _intent(customer_phone="+27821234567")
        business = _business()
        db = _FakeDB()
        with caplog.at_level(logging.INFO, logger="nextgen.payments.pos_link_whatsapp"):
            with patch(
                "backend.app.bot.whatsapp_sender.send_template_message",
                new_callable=AsyncMock,
            ) as mock_send:
                mock_send.return_value = ("wamid.masked", None)
                await send_pos_payment_link(db, intent, business, "https://pay.example/abc123")

        full_number = "27821234567"
        for record in caplog.records:
            assert full_number not in record.getMessage()
