"""
BUG-0067 — commercial invoice PDF 500s on any non-Latin-1 character (the
admin suite's own default subscription-line text contains an em dash).

Pure Python — no DB needed, `build_commercial_invoice_pdf` takes plain
attribute-access objects (SimpleNamespace stand-ins), same style as
tests/test_receipts_document_contract.py and tests/test_billing_account_status.py.

`pdf.set_compression(False)` (see commercial_invoice_pdf.py) means visible
text is grep-able directly in the raw, latin-1-decoded PDF bytes — no PDF
parsing dependency needed, matching the existing convention documented in
that module.
"""
import types
from datetime import date, datetime, timezone

import pytest

from backend.app.billing.commercial_invoice_pdf import _ascii_safe, build_commercial_invoice_pdf


def _business(**overrides):
    defaults = dict(
        name="Ciao Pizza", address="Lifestyle Centre, Ballito", phone="032 946 0000",
        vat_registered=True, vat_number="4123456789",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _line(**overrides):
    defaults = dict(
        description="Pro plan monthly subscription", quantity=1,
        unit_amount_cents=45000, line_total_cents=45000,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _invoice(**overrides):
    lines = overrides.pop("lines", None)
    if lines is None:
        lines = [_line()]
    defaults = dict(
        status="SENT", invoice_number="NG-INV-2026-00042",
        invoice_date=date(2026, 9, 1), due_date=date(2026, 9, 15),
        currency="ZAR", subtotal_cents=45000, vat_cents=0,
        vat_rate_basis_points=0, total_cents=45000,
        supplier_snapshot=None, customer_snapshot=None,
        paid_at=None, lines=lines,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestAsciiSafeSanitisation:
    def test_em_dash_becomes_hyphen(self):
        assert _ascii_safe("Pro plan — monthly subscription") == "Pro plan - monthly subscription"

    def test_en_dash_becomes_hyphen(self):
        assert _ascii_safe("2026–2027") == "2026-2027"

    def test_curly_quotes_become_straight_quotes(self):
        assert _ascii_safe("‘Cafe’ “Special”") == "'Cafe' \"Special\""

    def test_ellipsis_expands_to_three_dots(self):
        assert _ascii_safe("Loading…") == "Loading..."

    def test_accented_characters_transliterate_to_ascii(self):
        assert _ascii_safe("Café Élan") == "Cafe Elan"

    def test_none_becomes_empty_string(self):
        assert _ascii_safe(None) == ""

    def test_plain_ascii_is_unchanged(self):
        assert _ascii_safe("Ciao Pizza") == "Ciao Pizza"


class TestBuildCommercialInvoicePdfUnicodeSafety:
    def test_em_dash_in_line_description_does_not_raise_and_returns_pdf_bytes(self):
        """
        The exact string that crashed production: the admin suite's own
        default prefill (InvoiceEditorDialog.tsx) is
        f"{businessPlan} plan — monthly subscription".
        """
        invoice = _invoice(lines=[_line(description="Pro plan — monthly subscription")])
        business = _business()

        pdf_bytes = build_commercial_invoice_pdf(invoice, business)

        assert isinstance(pdf_bytes, bytes)
        assert pdf_bytes[:4] == b"%PDF"
        text = pdf_bytes.decode("latin-1", errors="replace")
        assert "Pro plan - monthly subscription" in text

    def test_accented_and_curly_quoted_restaurant_name_does_not_raise(self):
        """A restaurant name a real customer might actually have."""
        business = _business(name="Café ‘Du Soleil’ Élan")
        invoice = _invoice()

        pdf_bytes = build_commercial_invoice_pdf(invoice, business)

        assert pdf_bytes[:4] == b"%PDF"
        text = pdf_bytes.decode("latin-1", errors="replace")
        assert "Cafe 'Du Soleil' Elan" in text

    def test_invoice_number_and_amounts_still_render_correctly(self):
        """
        The sanitisation pass must not corrupt or drop the numbers/reference
        that actually matter on a tax document.
        """
        invoice = _invoice(
            invoice_number="NG-INV-2026-00042",
            lines=[_line(
                description="Pro plan — monthly subscription",
                quantity=2, unit_amount_cents=45000, line_total_cents=90000,
            )],
            subtotal_cents=90000, total_cents=90000,
        )
        business = _business()

        pdf_bytes = build_commercial_invoice_pdf(invoice, business)
        text = pdf_bytes.decode("latin-1", errors="replace")

        assert "NG-INV-2026-00042" in text
        assert "R 450.00" in text  # unit price
        assert "R 900.00" in text  # line total / subtotal / total due

    def test_paid_invoice_with_unicode_supplier_snapshot_renders(self):
        invoice = _invoice(
            status="PAID",
            paid_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
            supplier_snapshot={
                "legal_name": "NextGen (Pty) Ltd — Trading Division",
                "trading_name": "NextGen – Restaurant Platform",
                "address": "1 Main Rd… Suite 4",
                "contact_email": "billing@nextgen.example",
                "contact_phone": "0111234567",
                "vat_registered": True,
                "vat_number": "4987654321",
                "bank_name": "FNB",
                "bank_account_name": "NextGen (Pty) Ltd",
                "bank_account_number": "123456789",
                "bank_branch_code": "250655",
                "bank_account_type": "Cheque",
                "registration_number": "2024/123456/07",
            },
        )
        business = _business()

        pdf_bytes = build_commercial_invoice_pdf(invoice, business)

        assert pdf_bytes[:4] == b"%PDF"
        text = pdf_bytes.decode("latin-1", errors="replace")
        assert "PAID - 2026-09-10" in text
        # fpdf2 backslash-escapes literal "(" / ")" inside PDF string
        # literals (content is written as `(...\(Pty\) Ltd...) Tj`), so
        # search for the escaped form rather than the raw sanitised string.
        assert "NextGen \\(Pty\\) Ltd - Trading Division" in text


class TestBrokenPdfRendererRaisesForErrorHandlerTest:
    """
    Confirms the un-mocked failure mode still exists for a truly malformed
    input the sanitiser cannot help with (e.g. a non-string, non-None
    `.description` whose `str()` itself raises) — used to justify why a
    global exception handler is still needed as defense-in-depth even after
    the sanitisation fix (see tests/test_error_handlers_cors.py).
    """

    def test_object_whose_str_raises_propagates_as_exception(self):
        class Explode:
            def __str__(self):
                raise RuntimeError("boom")

        invoice = _invoice(lines=[_line(description=Explode())])
        business = _business()

        with pytest.raises(RuntimeError):
            build_commercial_invoice_pdf(invoice, business)
