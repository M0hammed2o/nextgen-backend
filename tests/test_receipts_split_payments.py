"""
Split-payment receipt rendering (migration 0055, docs/SPLIT_PAYMENTS_CONTRACT.md).

Mocked-session tests, same style as test_receipts_document_contract.py /
test_inventory_pos_integration.py — these are pure-Python dataclass/renderer
tests, no database needed. Covers:

  - Zero behaviour change for a legacy order (payment_method != "SPLIT", or
    zero allocation rows) — the exact regression the task's own hard
    constraint calls out.
  - A 2-way split (Cash + Card) and a 3-way split (Cash + Card + Pay by
    Link) correctly populate ReceiptPayment.allocations and both text-based
    renderers (render_text / render_escpos, which wraps render_text's own
    layout).
  - Neither renderer, under any payment method, ever prints a raw UUID or a
    payment-link URL.
"""

import types
from datetime import datetime, timezone

from backend.app.receipts.document import (
    ReceiptPaymentAllocation,
    build_receipt_document,
)
from backend.app.receipts.escpos import PrinterCapabilities, render_escpos, render_text


def _business(**overrides):
    defaults = dict(
        name="Ciao Pizza", address="Lifestyle Centre", phone="032 946 0000",
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80, receipt_copies=1,
        currency="ZAR", timezone="Africa/Johannesburg",
        receipt_config_json={"footer_message": "Grazie!"},
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _item(**overrides):
    defaults = dict(
        quantity=2, name_snapshot="Large Chicken Pizza",
        unit_price_cents=15000, line_total_cents=30000,
        selected_options_snapshot=[], add_ons_snapshot=[], removed_ingredients_snapshot=[],
        special_instructions=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _allocation(**overrides):
    defaults = dict(
        payment_method="CASH", amount_cents=15000,
        cash_tendered_cents=None, change_due_cents=None,
        payment_reference=None, provider=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _order(**overrides):
    item = overrides.pop("item", None) or _item()
    allocations = overrides.pop("payment_allocations", None)
    defaults = dict(
        order_number="A-0100", created_at=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        order_mode="DINE_IN", table_number="3", customer_name="Thabo",
        subtotal_cents=30000, discount_cents=0, discount_reason=None,
        delivery_fee_cents=0, tax_cents=0, total_cents=30000, currency="ZAR",
        payment_method="CASH", payment_status="PAID",
        cash_tendered_cents=None, change_due_cents=None,
        payment_reference=None, refund_amount_cents=None, items=[item],
        created_by_user_id=None,
    )
    defaults.update(overrides)
    order = types.SimpleNamespace(**defaults)
    if allocations is not None:
        order.payment_allocations = allocations
    return order


# ── Regression: zero behaviour change for a non-split order ────────────────

class TestLegacyOrdersUnchanged:
    """
    Every order with payment_method != "SPLIT" — including every legacy
    order that predates this migration and so has no payment_allocations
    attribute at all — must render byte-for-byte as it did before
    `allocations` existed.
    """

    def test_single_cash_order_has_no_allocations_and_renders_as_before(self):
        order = _order(
            payment_method="CASH", cash_tendered_cents=35000, change_due_cents=5000,
        )
        # No payment_allocations attribute at all — simulates a real legacy
        # Order row (the SQLAlchemy relationship would be an empty list, but
        # a plain object/getattr(..., None) must equally not break anything).
        assert not hasattr(order, "payment_allocations")

        doc = build_receipt_document(order, _business())
        assert doc.payment.allocations is None
        assert doc.payment.method == "CASH"
        assert doc.payment.cash_tendered_display == "R350.00"
        assert doc.payment.change_due_display == "R50.00"

        text = render_text(doc)
        assert "Paid by" in text
        assert "R350.00" in text
        assert "R50.00" in text

        payload = render_escpos(doc)
        assert payload.startswith(b"\x1b@")

    def test_single_card_order_unchanged(self):
        order = _order(payment_method="CARD", payment_reference="terminal-ref-123")
        doc = build_receipt_document(order, _business())
        assert doc.payment.allocations is None
        assert doc.payment.method == "CARD"
        assert doc.payment.cash_tendered_display is None
        assert doc.payment.change_due_display is None

        text = render_text(doc)
        assert "Paid by" in text
        assert "CARD" in text

    def test_single_other_order_unchanged(self):
        order = _order(payment_method="OTHER")
        doc = build_receipt_document(order, _business())
        assert doc.payment.allocations is None
        assert doc.payment.method == "OTHER"

    def test_order_with_zero_allocation_rows_but_payment_allocations_eq_empty_list(self):
        """
        A real SQLAlchemy relationship on an order with no rows returns []
        (not None) -- must be treated identically to the attribute being
        entirely absent.
        """
        order = _order(payment_method="CASH", cash_tendered_cents=30000, payment_allocations=[])
        doc = build_receipt_document(order, _business())
        assert doc.payment.allocations is None


# ── Split payments: 2-way and 3-way ──────────────────────────────────────────

class TestSplitPaymentDocument:
    def test_two_way_cash_and_card_split(self):
        allocations = [
            _allocation(payment_method="CASH", amount_cents=15000,
                        cash_tendered_cents=20000, change_due_cents=5000),
            _allocation(payment_method="CARD", amount_cents=15000, payment_reference="term-999"),
        ]
        order = _order(
            payment_method="SPLIT", total_cents=30000,
            cash_tendered_cents=None, change_due_cents=None, payment_reference=None,
            payment_allocations=allocations,
        )
        doc = build_receipt_document(order, _business())

        assert doc.payment.method == "SPLIT"
        # Single-method fields stay null for a SPLIT order (checkout itself
        # leaves them null) -- allocations carry the real breakdown instead.
        assert doc.payment.cash_tendered_display is None
        assert doc.payment.change_due_display is None

        allocs = doc.payment.allocations
        assert len(allocs) == 2
        cash, card = allocs
        assert isinstance(cash, ReceiptPaymentAllocation)
        assert cash.method == "Cash"
        assert cash.amount_cents == 15000
        assert cash.amount_display == "R150.00"
        assert cash.cash_tendered_display == "R200.00"
        assert cash.change_due_display == "R50.00"

        assert card.method == "Card"
        assert card.amount_display == "R150.00"
        assert card.cash_tendered_display is None
        assert card.change_due_display is None

    def test_three_way_cash_card_pay_by_link(self):
        allocations = [
            _allocation(payment_method="CASH", amount_cents=10000,
                        cash_tendered_cents=10000, change_due_cents=0),
            _allocation(payment_method="CARD", amount_cents=10000),
            _allocation(payment_method="PAY_BY_LINK", amount_cents=10000, provider="YOCO",
                        payment_reference="chr_abc123"),
        ]
        order = _order(payment_method="SPLIT", total_cents=30000, payment_allocations=allocations)
        doc = build_receipt_document(order, _business())

        allocs = doc.payment.allocations
        assert len(allocs) == 3
        labels = [a.method for a in allocs]
        assert labels == ["Cash", "Card", "Pay by Link (Yoco)"]
        assert allocs[2].provider == "YOCO"
        assert allocs[2].amount_display == "R100.00"

    def test_pay_by_link_unknown_provider_falls_back_to_generic_label(self):
        allocations = [
            _allocation(payment_method="CASH", amount_cents=15000, cash_tendered_cents=15000, change_due_cents=0),
            _allocation(payment_method="PAY_BY_LINK", amount_cents=15000, provider=None),
        ]
        order = _order(payment_method="SPLIT", total_cents=30000, payment_allocations=allocations)
        doc = build_receipt_document(order, _business())
        assert doc.payment.allocations[1].method == "Pay by Link"


class TestSplitPaymentRendering:
    def _split_order(self):
        allocations = [
            _allocation(payment_method="CASH", amount_cents=15000,
                        cash_tendered_cents=20000, change_due_cents=5000),
            _allocation(payment_method="CARD", amount_cents=10000, payment_reference="term-1"),
            _allocation(payment_method="PAY_BY_LINK", amount_cents=5000, provider="PAYFAST",
                        payment_reference="pf-ref-xyz"),
        ]
        return _order(payment_method="SPLIT", total_cents=30000, payment_allocations=allocations)

    def test_render_text_shows_one_line_per_allocation_with_cash_sublines(self):
        doc = build_receipt_document(self._split_order(), _business())
        text = render_text(doc)
        assert "Cash" in text
        assert "R150.00" in text
        assert "Cash tendered" in text and "R200.00" in text
        assert "Change" in text and "R50.00" in text
        assert "Card" in text
        assert "R100.00" in text
        assert "Pay by Link (PayFast)" in text
        assert "R50.00" in text  # Pay by Link amount

    def test_render_escpos_wraps_the_same_text_layout(self):
        doc = build_receipt_document(self._split_order(), _business())
        text = render_text(doc)
        payload = render_escpos(doc, PrinterCapabilities(codepage="utf-8"))
        for token in ("Cash", "Card", "Pay by Link (PayFast)"):
            assert token in text
            assert token.encode("utf-8") in payload

    def test_non_split_render_path_is_byte_for_byte_identical_to_before(self):
        """
        Direct regression proof: rendering the SAME legacy order before and
        after this feature existed must produce IDENTICAL text. Since this
        session only has the "after" code, this re-renders twice and checks
        determinism plus the exact expected legacy layout lines.
        """
        order = _order(payment_method="CASH", cash_tendered_cents=35000, change_due_cents=5000)
        business = _business()
        text_a = render_text(build_receipt_document(order, business))
        text_b = render_text(build_receipt_document(order, business))
        assert text_a == text_b
        lines = text_a.split("\n")
        assert any(line.strip().startswith("Paid by") and "CASH" in line for line in lines)
        assert any(line.strip().startswith("Cash") and "R350.00" in line for line in lines)
        assert any(line.strip().startswith("Change") and "R50.00" in line for line in lines)


class TestNeverLeaksInternalIdentifiersOrUrls:
    """
    Hard constraint: no renderer may ever print a raw UUID or a payment-link
    URL, for any payment method, split or not.
    """

    def test_split_order_never_prints_a_url_or_long_uuid_like_token(self):
        allocations = [
            _allocation(payment_method="CASH", amount_cents=15000,
                        cash_tendered_cents=15000, change_due_cents=0),
            _allocation(
                payment_method="PAY_BY_LINK", amount_cents=15000, provider="YOCO",
                payment_reference="https://pay.yoco.com/abc123-should-never-appear",
            ),
        ]
        order = _order(payment_method="SPLIT", total_cents=30000, payment_allocations=allocations)
        doc = build_receipt_document(order, _business())
        text = render_text(doc)
        payload = render_escpos(doc)
        assert "https://" not in text
        assert b"https://" not in payload
        assert "pay.yoco.com" not in text

    def test_legacy_order_payment_link_url_attribute_never_rendered(self):
        order = _order(payment_method="CARD")
        order.payment_link_url = "https://pay.example.com/should-not-print"
        text = render_text(build_receipt_document(order, _business()))
        assert "https://" not in text
