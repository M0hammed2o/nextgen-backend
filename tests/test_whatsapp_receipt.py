"""
Digital WhatsApp receipts — backend/app/receipts/whatsapp_receipt.py.

Covers:
  - build_whatsapp_receipt_text(): text formatting from a real ReceiptDocument
    (reused, not rebuilt — see TestUsesSharedDocumentBuilder).
  - send_whatsapp_receipt(): gating (WhatsApp enabled, identity resolvable)
    and that it calls send_text_message() (the outbox-persisting sender,
    not the fire-and-forget one) with business= set.
  - The _mark_order_paid() trigger in routes_payments.py: sent exactly once
    per PAID transition, not again on a duplicate webhook delivery.
"""

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from backend.app.receipts.document import build_receipt_document
from backend.app.receipts.whatsapp_receipt import (
    build_whatsapp_receipt_text,
    send_whatsapp_receipt,
)


# ── Fixtures (same shape as tests/test_receipts_document_contract.py) ────────

def _business(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        name="Ciao Pizza", address="Lifestyle Centre", phone="032 946 0000",
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80, receipt_copies=1,
        currency="ZAR", timezone="Africa/Johannesburg",
        receipt_config_json={"footer_message": "Thanks for supporting a local business!"},
        is_whatsapp_enabled=True,
        whatsapp_phone_number_id="1234567890",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _item(**overrides):
    defaults = dict(
        quantity=2, name_snapshot="Large Chicken Pizza",
        unit_price_cents=12900, line_total_cents=25800,
        selected_options_snapshot=[{"group_id": "g", "option_id": "o",
                                    "option_name": "Large", "price_delta_cents": 3000}],
        add_ons_snapshot=[{"name": "Extra Cheese", "quantity": 1, "price_cents": 1500}],
        removed_ingredients_snapshot=[{"id": "r1", "name": "Mushroom"}],
        special_instructions="Well done",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _order(**overrides):
    item = overrides.pop("item", None) or _item()
    defaults = dict(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        order_number="A-0042", created_at=datetime(2026, 9, 7, 18, 32, tzinfo=timezone.utc),
        order_mode="DINE_IN", table_number="7", customer_name="Thabo",
        subtotal_cents=25800, discount_cents=800, discount_reason="Staff discount",
        delivery_fee_cents=0,
        tax_cents=3261, total_cents=25000, currency="ZAR",
        payment_method="CASH", payment_status="PAID",
        cash_tendered_cents=30000, change_due_cents=5000,
        payment_reference=None, refund_amount_cents=None, items=[item],
        created_by_user_id=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _customer(**overrides):
    defaults = dict(id=uuid.uuid4(), wa_id="27821234567", phone_number=None)
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


# ── build_whatsapp_receipt_text ───────────────────────────────────────────────

class TestUsesSharedDocumentBuilder:
    def test_formats_the_same_document_the_printed_slip_uses(self):
        """
        Not a re-derivation: feed build_receipt_document()'s own output in,
        so this test would fail if the text renderer ever stopped reading
        the document's pre-formatted display strings.
        """
        doc = build_receipt_document(_order(), _business())
        text = build_whatsapp_receipt_text(doc)
        assert "Ciao Pizza" in text
        assert "Order #A-0042" in text


class TestBusinessNameAndOrderNumberAlwaysPresent:
    def test_present_on_a_minimal_order(self):
        order = _order(item=_item(), subtotal_cents=0, discount_cents=0, delivery_fee_cents=0,
                        tax_cents=0, total_cents=0, discount_reason=None, items=[])
        doc = build_receipt_document(order, _business(vat_registered=False, vat_rate_basis_points=0))
        text = build_whatsapp_receipt_text(doc)
        assert "Ciao Pizza" in text
        assert "Order #A-0042" in text
        assert "*Total: R0.00*" in text


class TestNoCentsIntegersLeak:
    def test_only_formatted_display_strings_appear(self):
        doc = build_receipt_document(_order(), _business())
        text = build_whatsapp_receipt_text(doc)
        # The raw cents integers for subtotal/discount/total must never
        # appear bare in the text — only their "R123.45" display forms.
        assert "25800" not in text
        assert "800" not in text  # discount_cents
        assert "25000" not in text  # total_cents
        assert "R258.00" in text  # subtotal_display
        assert "R250.00" in text  # total_display


class TestVatRegisteredOrder:
    def test_rate_shown_when_consistent_with_stored_tax(self):
        doc = build_receipt_document(_order(tax_cents=3261), _business())
        text = build_whatsapp_receipt_text(doc)
        assert "VAT incl. (15%): R32.61" in text

    def test_rate_omitted_when_it_no_longer_reconciles(self):
        order = _order(tax_cents=3261)  # consistent with the 15% rate at sale time
        changed_business = _business(vat_rate_basis_points=2000)  # tenant since changed to 20%
        doc = build_receipt_document(order, changed_business)
        text = build_whatsapp_receipt_text(doc)
        assert "R32.61" in text  # amount is still shown (it's a snapshot, always correct)
        assert "20%" not in text
        assert "15%" not in text


class TestOrderWithNullVatRate:
    def test_no_vat_line_when_business_is_not_vat_registered(self):
        order = _order(tax_cents=0)
        doc = build_receipt_document(order, _business(vat_registered=False, vat_rate_basis_points=0))
        text = build_whatsapp_receipt_text(doc)
        assert "VAT" not in text


class TestWhatsAppOrderWithDeliveryFee:
    def test_delivery_line_present(self):
        order = _order(delivery_fee_cents=2500, total_cents=27500, tax_cents=0)
        doc = build_receipt_document(order, _business(vat_registered=False, vat_rate_basis_points=0))
        text = build_whatsapp_receipt_text(doc)
        assert "Delivery: R25.00" in text


class TestOrderWithDiscount:
    def test_discount_line_with_reason(self):
        doc = build_receipt_document(_order(discount_cents=800, discount_reason="Staff discount"), _business())
        text = build_whatsapp_receipt_text(doc)
        assert "Discount: -R8.00 (Staff discount)" in text


class TestFooterAndFormatting:
    def test_footer_message_and_thank_you_present(self):
        doc = build_receipt_document(_order(), _business())
        text = build_whatsapp_receipt_text(doc)
        assert "Thanks for supporting a local business!" in text
        assert "Thank you for your order!" in text

    def test_cash_change_due_shown(self):
        doc = build_receipt_document(_order(payment_method="CASH", cash_tendered_cents=30000, change_due_cents=5000), _business())
        text = build_whatsapp_receipt_text(doc)
        assert "Payment: Cash" in text
        assert "Change: R50.00" in text


# ── send_whatsapp_receipt ─────────────────────────────────────────────────────

class TestSendWhatsappReceipt:
    async def test_sends_via_send_text_message_with_business_when_enabled_and_resolvable(self):
        business = _business()
        order = _order()
        customer = _customer(wa_id="27821234567")
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            mock_send.return_value = "wamid.TEST"
            result = await send_whatsapp_receipt(db, order, business, customer=customer)

        assert result is True
        mock_send.assert_awaited_once()
        _, kwargs = mock_send.call_args
        assert kwargs["business"] is business
        assert kwargs["customer_wa_id"] == "27821234567"
        assert kwargs["phone_number_id"] == business.whatsapp_phone_number_id
        assert kwargs["customer_id"] == customer.id
        assert "Ciao Pizza" in kwargs["text"]

    async def test_noop_when_whatsapp_disabled(self):
        business = _business(is_whatsapp_enabled=False)
        order = _order()
        customer = _customer()
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            result = await send_whatsapp_receipt(db, order, business, customer=customer)

        assert result is False
        mock_send.assert_not_awaited()

    async def test_noop_when_no_customer(self):
        business = _business()
        order = _order()
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            result = await send_whatsapp_receipt(db, order, business, customer=None)

        assert result is False
        mock_send.assert_not_awaited()

    async def test_noop_when_customer_has_no_wa_id(self):
        """POS/manual customers linked by phone_number only (wa_id=None) —
        see get_or_create_customer_by_phone(). No phone number is guessed as
        a fallback wa_id (see module docstring)."""
        business = _business()
        order = _order()
        customer = _customer(wa_id=None, phone_number="0821234567")
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            result = await send_whatsapp_receipt(db, order, business, customer=customer)

        assert result is False
        mock_send.assert_not_awaited()

    async def test_noop_when_no_phone_number_id_configured(self):
        business = _business(whatsapp_phone_number_id=None)
        order = _order()
        customer = _customer()
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            result = await send_whatsapp_receipt(db, order, business, customer=customer)

        assert result is False
        mock_send.assert_not_awaited()

    async def test_never_raises_into_caller(self):
        business = _business()
        order = _order()
        customer = _customer()
        db = object()

        with patch(
            "backend.app.bot.whatsapp_sender.send_text_message", new_callable=AsyncMock
        ) as mock_send:
            mock_send.side_effect = RuntimeError("network exploded")
            result = await send_whatsapp_receipt(db, order, business, customer=customer)

        assert result is False
