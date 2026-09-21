"""
Commercial (EFT/cash) invoice PDF renderer — the client-facing document for
`shared.models.commercial_invoice.CommercialInvoice`. Reuses the same fpdf2
library and visual conventions already proven in
`admin_api/app/api/v1/routes_businesses.py::generate_billing_pdf` (a
different, internal-only billing-summary document — this one is a real,
numbered South African tax/commercial document).

Lives under `backend/app/billing/` (not `admin_api/app/services/`) even
though today only `admin_api` calls it, so BOTH the admin-plane invoice
routes (`admin_api/app/api/v1/routes_commercial.py`) and the owner-facing
PDF download (`backend/app/api/v1/routes_billing.py`) can import it without
either plane importing the other — `admin_api` already depends on
`backend`, never the reverse.

All money is formatted directly from integer cents — never a float.
"""

from datetime import datetime, timezone

from fpdf import FPDF

from backend.app.core.config import get_settings
from shared.models.business import Business
from shared.models.commercial_invoice import CommercialInvoice


def _rands(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}R {cents // 100:,}.{cents % 100:02d}"


def build_commercial_invoice_pdf(invoice: CommercialInvoice, business: Business) -> bytes:
    settings = get_settings()
    is_paid = invoice.status == "PAID"
    is_cancelled = invoice.status == "CANCELLED"
    is_draft = invoice.status == "DRAFT"

    supplier = invoice.supplier_snapshot or {
        "legal_name": settings.NEXTGEN_LEGAL_NAME or "(NextGen legal name not yet configured)",
        "trading_name": settings.NEXTGEN_TRADING_NAME,
        "registration_number": settings.NEXTGEN_REGISTRATION_NUMBER,
        "address": settings.NEXTGEN_ADDRESS or "(address not yet configured)",
        "contact_email": settings.NEXTGEN_CONTACT_EMAIL,
        "contact_phone": settings.NEXTGEN_CONTACT_PHONE,
        "vat_registered": settings.NEXTGEN_VAT_REGISTERED,
        "vat_number": settings.NEXTGEN_VAT_NUMBER,
        "bank_name": settings.NEXTGEN_BANK_NAME or "(not yet configured)",
        "bank_account_name": settings.NEXTGEN_BANK_ACCOUNT_NAME or "(not yet configured)",
        "bank_account_number": settings.NEXTGEN_BANK_ACCOUNT_NUMBER or "(not yet configured)",
        "bank_branch_code": settings.NEXTGEN_BANK_BRANCH_CODE or "(not yet configured)",
        "bank_account_type": settings.NEXTGEN_BANK_ACCOUNT_TYPE,
        "payment_terms_days": settings.NEXTGEN_INVOICE_PAYMENT_TERMS_DAYS,
    }
    customer = invoice.customer_snapshot or {
        "name": business.name, "address": business.address, "phone": business.phone,
        "vat_registered": business.vat_registered, "vat_number": business.vat_number,
    }

    title = "TAX INVOICE" if supplier.get("vat_registered") else "INVOICE"

    pdf = FPDF()
    # Uncompressed content streams — this document is small, and leaving it
    # uncompressed lets a caller/test grep the raw PDF bytes for the invoice
    # number, DRAFT banner or PAID stamp without a PDF-parsing dependency
    # (fpdf2 defaults to zlib-compressed streams, which would otherwise hide
    # all visible text from a plain byte search).
    pdf.set_compression(False)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    if is_draft:
        pdf.set_fill_color(255, 235, 205)
        pdf.set_text_color(180, 90, 0)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 9, "DRAFT - NOT A VALID INVOICE", new_x="LMARGIN", new_y="NEXT", fill=True, align="C")
        pdf.ln(3)

    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(30, 64, 175)
    pdf.cell(0, 10, supplier.get("trading_name") or supplier.get("legal_name") or "NextGen", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    if supplier.get("legal_name"):
        pdf.cell(0, 5, str(supplier["legal_name"]), new_x="LMARGIN", new_y="NEXT")
    if supplier.get("address"):
        pdf.multi_cell(0, 5, str(supplier["address"]))
    contact_bits = [b for b in (supplier.get("contact_email"), supplier.get("contact_phone")) if b]
    if contact_bits:
        pdf.cell(0, 5, " | ".join(contact_bits), new_x="LMARGIN", new_y="NEXT")
    if supplier.get("vat_registered") and supplier.get("vat_number"):
        pdf.cell(0, 5, f"VAT No: {supplier['vat_number']}", new_x="LMARGIN", new_y="NEXT")
    if supplier.get("registration_number"):
        pdf.cell(0, 5, f"Reg No: {supplier['registration_number']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Invoice No: {invoice.invoice_number or '(not yet issued)'}", new_x="LMARGIN", new_y="NEXT")
    if invoice.invoice_date:
        pdf.cell(0, 6, f"Invoice Date: {invoice.invoice_date.isoformat()}", new_x="LMARGIN", new_y="NEXT")
    if invoice.due_date:
        pdf.cell(0, 6, f"Due Date: {invoice.due_date.isoformat()}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "Bill To:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, str(customer.get("name") or ""), new_x="LMARGIN", new_y="NEXT")
    if customer.get("address"):
        pdf.multi_cell(0, 5, str(customer["address"]))
    if customer.get("phone"):
        pdf.cell(0, 5, str(customer["phone"]), new_x="LMARGIN", new_y="NEXT")
    if customer.get("vat_registered") and customer.get("vat_number"):
        pdf.cell(0, 5, f"VAT No: {customer['vat_number']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── Line items ───────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(240, 240, 245)
    pdf.cell(90, 7, "Description", border=1, fill=True)
    pdf.cell(20, 7, "Qty", border=1, fill=True, align="R")
    pdf.cell(35, 7, "Unit Price", border=1, fill=True, align="R")
    pdf.cell(35, 7, "Line Total", border=1, fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    for line in invoice.lines:
        pdf.cell(90, 7, str(line.description)[:60], border=1)
        pdf.cell(20, 7, str(line.quantity), border=1, align="R")
        pdf.cell(35, 7, _rands(line.unit_amount_cents), border=1, align="R")
        pdf.cell(35, 7, _rands(line.line_total_cents), border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(145, 6, "Subtotal", align="R")
    pdf.cell(35, 6, _rands(invoice.subtotal_cents), align="R", new_x="LMARGIN", new_y="NEXT")
    if invoice.vat_cents:
        pdf.cell(145, 6, f"VAT ({invoice.vat_rate_basis_points / 100:.2f}%)", align="R")
        pdf.cell(35, 6, _rands(invoice.vat_cents), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(145, 8, "Total Due", align="R")
    pdf.cell(35, 8, _rands(invoice.total_cents), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # ── Payment instructions ────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 7, "Payment Instructions (EFT)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Bank: {supplier.get('bank_name')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Account Name: {supplier.get('bank_account_name')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Account Number: {supplier.get('bank_account_number')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Branch Code: {supplier.get('bank_branch_code')}", new_x="LMARGIN", new_y="NEXT")
    if supplier.get("bank_account_type"):
        pdf.cell(0, 5, f"Account Type: {supplier['bank_account_type']}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 6, f"Please use invoice number {invoice.invoice_number or ''} as your payment reference.", new_x="LMARGIN", new_y="NEXT")

    # ── Status stamps ────────────────────────────────────────────────────
    if is_paid:
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(0, 140, 0)
        paid_date = invoice.paid_at.strftime("%Y-%m-%d") if invoice.paid_at else ""
        pdf.cell(0, 12, f"PAID - {paid_date}", new_x="LMARGIN", new_y="NEXT", align="C")
    if is_cancelled:
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(180, 0, 0)
        pdf.cell(0, 12, "CANCELLED", new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.set_text_color(150, 150, 150)
    pdf.set_font("Helvetica", "I", 8)
    pdf.ln(8)
    pdf.cell(0, 5, f"Generated {datetime.now(timezone.utc).strftime('%d %B %Y at %H:%M UTC')}", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
