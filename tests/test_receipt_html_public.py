"""
Public receipt HTML renderer — backend/app/receipts/html_public.py.

Mocked-session, no DB: feeds build_receipt_document()'s own real output in
(not a re-derivation) so this would fail if the HTML renderer ever stopped
reading the document's pre-formatted display strings, same pattern as
tests/test_receipts_document_contract.py and the old whatsapp text-renderer
tests. Also covers HTML-escaping of untrusted content and the standalone
render_not_found_html() 404 page.
"""

import types
from datetime import datetime, timezone

from backend.app.receipts.document import build_receipt_document
from backend.app.receipts.html_public import render_not_found_html, render_public_receipt_html


def _business(**overrides):
    defaults = dict(
        name="Ciao Pizza", address="Lifestyle Centre", phone="032 946 0000",
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80, receipt_copies=1,
        currency="ZAR", timezone="Africa/Johannesburg",
        receipt_config_json={"footer_message": "Thanks for supporting a local business!"},
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _item(**overrides):
    defaults = dict(
        quantity=2, name_snapshot="Large Chicken Pizza",
        unit_price_cents=12900, line_total_cents=25800,
        selected_options_snapshot=[{"group_id": "g", "option_id": "o",
                                    "option_name": "Large", "price_delta_cents": 3000}],
        add_ons_snapshot=[], removed_ingredients_snapshot=[],
        special_instructions=None,
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


class TestUsesSharedDocumentBuilder:
    def test_renders_the_same_document_the_printed_slip_uses(self):
        doc = build_receipt_document(_order(), _business())
        html = render_public_receipt_html(doc)
        assert "Ciao Pizza" in html
        assert "#A-0042" in html
        assert "<!DOCTYPE html>" in html


class TestNoExternalResourcesExceptOptionalLogo:
    def test_no_script_tags_no_external_stylesheet_links(self):
        doc = build_receipt_document(_order(), _business())
        html = render_public_receipt_html(doc)
        assert "<script" not in html
        assert '<link rel="stylesheet"' not in html

    def test_logo_only_present_when_configured(self):
        doc_without = build_receipt_document(_order(), _business())
        assert "<img" not in render_public_receipt_html(doc_without)

        doc_with = build_receipt_document(
            _order(), _business(receipt_config_json={"logo_url": "https://cdn.example.com/logo.png"}),
        )
        html_with = render_public_receipt_html(doc_with)
        assert '<img class="logo" src="https://cdn.example.com/logo.png"' in html_with


class TestHtmlEscaping:
    def test_trading_name_with_html_special_chars_is_escaped(self):
        doc = build_receipt_document(
            _order(), _business(receipt_config_json={"trading_name": "<b>Ciao</b> & Co"}),
        )
        html = render_public_receipt_html(doc)
        assert "<b>Ciao</b>" not in html
        assert "&lt;b&gt;Ciao&lt;/b&gt; &amp; Co" in html

    def test_item_name_with_script_tag_is_escaped(self):
        malicious_item = _item(name_snapshot="<script>alert(1)</script>")
        doc = build_receipt_document(_order(item=malicious_item), _business())
        html = render_public_receipt_html(doc)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_footer_message_is_escaped(self):
        doc = build_receipt_document(
            _order(), _business(receipt_config_json={"footer_message": "<img src=x onerror=alert(1)>"}),
        )
        html = render_public_receipt_html(doc)
        assert "<img src=x onerror=alert(1)>" not in html


class TestMoneyAndBanner:
    def test_total_and_subtotal_display_strings_present(self):
        doc = build_receipt_document(_order(), _business())
        html = render_public_receipt_html(doc)
        assert "R258.00" in html  # subtotal_display
        assert "R250.00" in html  # total_display

    def test_refunded_banner_shown(self):
        doc = build_receipt_document(
            _order(payment_status="REFUNDED", refund_amount_cents=1000), _business(),
        )
        html = render_public_receipt_html(doc)
        assert "REFUNDED" in html


class TestPoweredByLine:
    def test_powered_by_nextgen_present(self):
        doc = build_receipt_document(_order(), _business())
        html = render_public_receipt_html(doc)
        assert "Powered by NextGen Intelligence" in html


class TestNotFoundPage:
    def test_generic_message_no_sensitive_details(self):
        html = render_not_found_html()
        assert "Receipt not found" in html
        assert "<!DOCTYPE html>" in html

    def test_stable_across_calls(self):
        assert render_not_found_html() == render_not_found_html()


class TestSplitPaymentBreakdown:
    """
    Split payments (migration 0055) — this renderer was missed in the
    original pass (its file list only covered JSON/ESC-POS/plain-text/
    WhatsApp) even though the task required every real receipt renderer,
    including the public customer-facing page, to show a split breakdown.
    """

    def _allocation(self, **overrides):
        defaults = dict(
            payment_method="CASH", amount_cents=15000,
            cash_tendered_cents=None, change_due_cents=None,
            payment_reference=None, provider=None,
        )
        defaults.update(overrides)
        return types.SimpleNamespace(**defaults)

    def test_legacy_single_cash_order_unchanged(self):
        doc = build_receipt_document(_order(payment_method="CASH"), _business())
        html = render_public_receipt_html(doc)
        assert "Cash tendered" in html
        assert "Change" in html

    def test_split_order_shows_one_line_per_allocation_with_cash_sublines(self):
        order = _order(
            payment_method="SPLIT", total_cents=30000,
            cash_tendered_cents=None, change_due_cents=None, payment_reference=None,
        )
        order.payment_allocations = [
            self._allocation(payment_method="CASH", amount_cents=15000,
                              cash_tendered_cents=20000, change_due_cents=5000),
            self._allocation(payment_method="PAY_BY_LINK", amount_cents=15000,
                              provider="YOCO", payment_reference="chr_abc123"),
        ]
        doc = build_receipt_document(order, _business())
        html = render_public_receipt_html(doc)

        assert "Cash" in html
        assert "R150.00" in html
        assert "Cash tendered" in html and "R200.00" in html
        assert "Change" in html and "R50.00" in html
        assert "Pay by Link (Yoco)" in html
        # Never the raw provider transaction reference or any internal id.
        assert "chr_abc123" not in html

    def test_split_order_never_leaks_payment_method_single_line(self):
        """A SPLIT order must not ALSO print the generic single-method
        "Payment method" row — only the per-allocation breakdown."""
        order = _order(payment_method="SPLIT", total_cents=30000,
                        cash_tendered_cents=None, change_due_cents=None, payment_reference=None)
        order.payment_allocations = [
            self._allocation(payment_method="CASH", amount_cents=15000, cash_tendered_cents=15000),
            self._allocation(payment_method="CARD", amount_cents=15000, payment_reference="term-1"),
        ]
        doc = build_receipt_document(order, _business())
        html = render_public_receipt_html(doc)
        assert "Payment method" not in html
