"""
Receipt document JSON contract — mocked-session tests.

tests/test_inventory_pos_integration.py already covers rendering (text/
ESC/POS layout, VAT show/hide, banners). This file covers the parts of the
Wave 0A "single source of truth" contract that are specific to the JSON
document a frontend will consume directly: pre-formatted money display
strings (so the frontend never re-does currency arithmetic), the VAT-rate
consistency rule, business-timezone localisation, discount reason, and
reprint metadata. Real-DB proof that these hold against an actual Order
row (not a live menu/business recomputation) lives in
tests/integration/test_receipts_realdb.py.
"""

import types
from datetime import datetime, timezone

from backend.app.receipts import escpos as escpos_module
from backend.app.receipts.document import (
    build_receipt_document,
    format_money,
)
from backend.app.receipts.escpos import render_text


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


class TestFormatMoneySharedAcrossRenderers:
    def test_escpos_reexports_the_same_function_document_uses(self):
        """
        The JSON document's display strings and the text/ESC/POS renderers
        must go through one implementation, or they can silently drift
        apart. Not just "same output" — the actual same function object.
        """
        assert escpos_module.format_money is format_money


class TestMoneyDisplayFields:
    """
    Every money value on the document must also carry a pre-formatted
    string, so a frontend can lay a slip out without doing currency
    arithmetic or reformatting integer cents itself.
    """

    def test_line_and_modifier_display_strings(self):
        doc = build_receipt_document(_order(), _business())
        line = doc.lines[0]
        assert line.unit_amount_cents == 12900
        assert line.unit_amount_display == "R129.00"
        assert line.amount_cents == 25800
        assert line.amount_display == "R258.00"

        by_label = {m.label: m for m in line.modifiers}
        assert by_label["Large"].amount_display == "R30.00"
        assert by_label["+ Extra Cheese"].amount_display == "R15.00"
        # A removal has no price and so no display string either.
        assert by_label["No Mushroom"].amount_cents is None
        assert by_label["No Mushroom"].amount_display is None

    def test_totals_display_strings(self):
        totals = build_receipt_document(_order(), _business()).totals
        assert totals.subtotal_display == "R258.00"
        assert totals.discount_display == "-R8.00"
        assert totals.discount_reason == "Staff discount"
        assert totals.total_display == "R250.00"
        assert totals.vat_display == f"R{3261 / 100:.2f}"

    def test_delivery_fee_display_only_present_when_charged(self):
        no_delivery = build_receipt_document(_order(), _business()).totals
        assert no_delivery.delivery_fee_display is None

        with_delivery = build_receipt_document(
            _order(delivery_fee_cents=2500), _business()
        ).totals
        assert with_delivery.delivery_fee_display == "R25.00"

    def test_payment_display_strings_are_null_safe(self):
        cash_order = build_receipt_document(_order(), _business()).payment
        assert cash_order.cash_tendered_display == "R300.00"
        assert cash_order.change_due_display == "R50.00"
        assert cash_order.refund_amount_display is None  # nothing refunded

        card_order = build_receipt_document(
            _order(payment_method="CARD", cash_tendered_cents=None, change_due_cents=None),
            _business(),
        ).payment
        assert card_order.cash_tendered_display is None
        assert card_order.change_due_display is None

    def test_refund_amount_display(self):
        payment = build_receipt_document(
            _order(payment_status="PARTIALLY_REFUNDED", refund_amount_cents=1200), _business()
        ).payment
        assert payment.refund_amount_cents == 1200
        assert payment.refund_amount_display == "R12.00"


class TestVatRateConsistencyRule:
    """
    Business.vat_rate_basis_points/vat_inclusive_pricing are LIVE columns,
    not a snapshot on the order — see document.py's _vat_rate_display()
    docstring. The rate must only be printed when it still reconciles with
    the order's own stored tax_cents.
    """

    def test_rate_shown_when_it_reconciles_with_stored_tax(self):
        # subtotal 25800 - discount 800 + delivery 0 = net 25000
        # inclusive 15% of 25000 = 3260.87 -> rounds to 3261, matching tax_cents below.
        totals = build_receipt_document(_order(tax_cents=3261), _business()).totals
        assert totals.vat_rate_basis_points == 1500
        assert totals.vat_inclusive is True

    def test_rate_omitted_when_business_rate_changed_since_the_sale(self):
        """
        Simulates a tenant changing its VAT rate after an old order was
        placed: the stored tax_cents still reflects the OLD rate, but the
        live Business row now reports a different one.
        """
        order = _order(tax_cents=3261)  # consistent with 15% inclusive
        changed_business = _business(vat_rate_basis_points=2000)  # now 20%

        totals = build_receipt_document(order, changed_business).totals
        assert totals.vat_cents == 3261  # unchanged — always correct, from the snapshot
        assert totals.vat_rate_basis_points is None
        assert totals.vat_inclusive is None

        # The text renderer must still show a usable (if rate-less) VAT
        # line, never a rate that contradicts the printed amount.
        text = render_text(build_receipt_document(order, changed_business))
        assert "VAT" in text
        assert "20.00%" not in text
        assert "15.00%" not in text
        assert "R32.61" in text

    def test_rate_omitted_when_vat_inclusive_flag_changed_since_the_sale(self):
        order = _order(tax_cents=3261)  # was inclusive at sale time
        changed_business = _business(vat_inclusive_pricing=False)  # tenant switched to exclusive

        totals = build_receipt_document(order, changed_business).totals
        assert totals.vat_rate_basis_points is None
        assert totals.vat_inclusive is None

    def test_no_vat_no_rate_fields_at_all(self):
        totals = build_receipt_document(
            _order(tax_cents=0), _business(vat_registered=False, vat_number=None)
        ).totals
        assert totals.vat_cents == 0
        assert totals.vat_display is None
        assert totals.vat_rate_basis_points is None
        assert totals.vat_inclusive is None


class TestTimezoneLocalisation:
    def test_issued_at_local_uses_business_timezone_not_utc(self):
        # Africa/Johannesburg is UTC+2 year-round (no DST).
        order = _order(created_at=datetime(2026, 9, 7, 18, 32, tzinfo=timezone.utc))
        meta = build_receipt_document(order, _business(timezone="Africa/Johannesburg")).meta
        assert meta.issued_at == "2026-09-07T18:32:00+00:00"
        assert meta.issued_at_local == "2026-09-07 20:32"

    def test_missing_timezone_falls_back_without_raising(self):
        business_no_tz = _business()
        del business_no_tz.timezone
        order = _order()
        meta = build_receipt_document(order, business_no_tz).meta
        assert meta.issued_at_local is not None  # falls back to platform default tz, never crashes

    def test_invalid_timezone_falls_back_without_raising(self):
        order = _order()
        meta = build_receipt_document(order, _business(timezone="Not/AZone")).meta
        assert meta.issued_at_local is not None


class TestReprintMetadata:
    def test_reprint_count_and_last_reprinted_at_pass_through(self):
        meta = build_receipt_document(
            _order(), _business(), reprint_count=3, last_reprinted_at="2026-09-20T10:00:00+00:00"
        ).meta
        assert meta.reprint_count == 3
        assert meta.last_reprinted_at == "2026-09-20T10:00:00+00:00"

    def test_defaults_to_zero_and_none(self):
        meta = build_receipt_document(_order(), _business()).meta
        assert meta.reprint_count == 0
        assert meta.last_reprinted_at is None


class TestKitchenDocumentCarriesNoPrices:
    def test_kitchen_lines_have_no_amount_display(self):
        doc = build_receipt_document(_order(), _business(), document_type="KITCHEN")
        line = doc.lines[0]
        assert line.amount_cents == 0
        assert line.amount_display == ""
        assert line.unit_amount_cents == 0
        assert line.unit_amount_display == ""


class TestLongContentNeverOverflowsPaperWidth:
    """
    BUG-0036 was a one-character overflow in two_column(); this exercises
    the fuller slip (long name, long modifier label, long note) at both
    paper widths, not just the isolated helper.
    """

    def test_long_names_modifiers_and_notes_stay_within_columns(self):
        long_item = _item(
            name_snapshot="Extra Large Deep Dish Hawaiian Pizza With Double Pepperoni And Jalapenos",
            selected_options_snapshot=[{
                "option_name": "Family Size Extra Thick Crust With Stuffed Cheese Border",
                "price_delta_cents": 5000,
            }],
            add_ons_snapshot=[{
                "name": "Triple Extra Mozzarella And Parmesan Cheese Blend", "quantity": 3,
                "price_cents": 700,
            }],
            special_instructions="Please cut into 12 slices and box separately, no garlic, extra napkins",
        )
        for width, columns in ((58, 32), (80, 48)):
            doc = build_receipt_document(
                _order(item=long_item), _business(receipt_paper_width_mm=width)
            )
            for line in render_text(doc).split("\n"):
                assert len(line) <= columns, f"{width}mm line overflowed: {line!r}"
