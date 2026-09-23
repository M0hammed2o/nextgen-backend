"""
Public receipt page — the fifth renderer of the one ReceiptDocument
(backend/app/receipts/document.py::build_receipt_document()). JSON/PWA,
ESC/POS, plain text and WhatsApp-template already exist; this is
"self-contained mobile HTML", served by the unauthenticated
GET /v1/r/{token} route (backend/app/api/v1/routes_receipt_public.py).

NO EXTERNAL CSS/JS, NO FRAMEWORK
---------------------------------
This page is reached by an unauthenticated customer, often on a slow mobile
connection, straight off a WhatsApp link. It must render correctly with
zero round trips beyond the one HTML response — every style is inlined in a
single <style> block, there is no JavaScript at all, and the only external
resource that can ever be requested is the business's own logo image (a
URL, never embedded binary — see ReceiptHeader.logo_url), which degrades
gracefully (a broken/slow image never blocks the rest of the receipt from
rendering, since it is a normal <img> tag with its own failure handling by
the browser).

ALL CONTENT IS ESCAPED
-----------------------
Every string field on the document (trading name, item names, modifier
labels, footer message, etc.) passed through html.escape() before being
interpolated — this data ultimately traces back to what a restaurant owner
typed into their own menu/settings, or what a customer typed as an order
note, and none of it should ever be trusted as pre-sanitised HTML.
"""

from __future__ import annotations

from html import escape

from backend.app.receipts.document import ReceiptDocument

_STYLE = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: #f4f4f5; color: #18181b;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 15px; line-height: 1.45;
  }
  .receipt {
    max-width: 420px; margin: 0 auto; background: #fff; border-radius: 10px;
    padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
  }
  .banner {
    text-align: center; font-weight: 700; letter-spacing: 0.04em;
    background: #fef2f2; color: #b91c1c; border-radius: 6px;
    padding: 6px 10px; margin-bottom: 12px; text-transform: uppercase;
  }
  .logo { display: block; max-width: 160px; max-height: 80px; margin: 0 auto 10px; }
  .header { text-align: center; margin-bottom: 14px; }
  .header .trading-name { font-size: 19px; font-weight: 700; }
  .header .line { color: #52525b; font-size: 13px; }
  .meta { font-size: 13px; color: #3f3f46; margin-bottom: 14px; }
  .meta div { display: flex; justify-content: space-between; }
  hr { border: none; border-top: 1px dashed #d4d4d8; margin: 12px 0; }
  .line-item { margin-bottom: 8px; }
  .line-item .row { display: flex; justify-content: space-between; font-weight: 600; }
  .line-item .modifier { display: flex; justify-content: space-between; font-size: 13px; color: #52525b; padding-left: 10px; }
  .line-item .note { font-size: 13px; color: #71717a; padding-left: 10px; font-style: italic; }
  .totals div, .payment div { display: flex; justify-content: space-between; }
  .totals .grand-total { font-weight: 700; font-size: 17px; margin-top: 6px; }
  .footer { text-align: center; margin-top: 16px; color: #52525b; font-size: 13px; }
  .powered-by { text-align: center; margin-top: 18px; color: #a1a1aa; font-size: 11px; }
""".strip()

_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipt not found</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: #f4f4f5; color: #3f3f46; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; padding: 24px; text-align: center; }
  .box { max-width: 360px; }
  h1 { font-size: 18px; margin-bottom: 8px; }
  p { font-size: 14px; color: #71717a; }
</style>
</head>
<body>
  <div class="box">
    <h1>Receipt not found</h1>
    <p>This link is invalid or has expired. Please contact the restaurant if you need a copy of your receipt.</p>
  </div>
</body>
</html>
"""


def render_not_found_html() -> str:
    """
    The one generic 404 page for every failure mode of GET /v1/r/{token} —
    unknown token, malformed token, or any internal error. Deliberately
    identical in every case so a cross-tenant/enumeration probe learns
    nothing about which case it hit (same principle as
    backend/app/core/ownership.py::require_owned for the authenticated side).
    """
    return _NOT_FOUND_HTML


def _money_row(label: str, value: str | None, *, css_class: str = "") -> str:
    if value is None:
        return ""
    cls = f' class="{css_class}"' if css_class else ""
    return f"<div{cls}><span>{escape(label)}</span><span>{escape(value)}</span></div>"


def render_public_receipt_html(document: ReceiptDocument) -> str:
    header = document.header
    meta = document.meta
    totals = document.totals
    payment = document.payment
    footer = document.footer

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    title = escape(header.trading_name or "Receipt")
    parts.append(f"<title>Receipt — {title}</title>")
    parts.append(f"<style>{_STYLE}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append('<div class="receipt">')

    if document.banner:
        parts.append(f'<div class="banner">{escape(document.banner)}</div>')

    parts.append('<div class="header">')
    if header.logo_url:
        parts.append(f'<img class="logo" src="{escape(header.logo_url)}" alt="">')
    if header.trading_name:
        parts.append(f'<div class="trading-name">{escape(header.trading_name)}</div>')
    if header.branch_name:
        parts.append(f'<div class="line">{escape(header.branch_name)}</div>')
    if header.address:
        parts.append(f'<div class="line">{escape(header.address)}</div>')
    if header.phone:
        parts.append(f'<div class="line">{escape(header.phone)}</div>')
    if header.vat_number:
        parts.append(f'<div class="line">VAT No: {escape(header.vat_number)}</div>')
    if footer.show_vat_invoice_label:
        parts.append('<div class="line">TAX INVOICE</div>')
    for extra in header.extra_lines:
        parts.append(f'<div class="line">{escape(str(extra))}</div>')
    parts.append("</div>")  # .header

    parts.append('<div class="meta">')
    if meta.order_number:
        parts.append(f"<div><span>Order</span><span>#{escape(meta.order_number)}</span></div>")
    if meta.issued_at_local:
        parts.append(f"<div><span>Date</span><span>{escape(meta.issued_at_local)}</span></div>")
    if meta.order_type:
        parts.append(f"<div><span>Type</span><span>{escape(meta.order_type)}</span></div>")
    if meta.table_number:
        parts.append(f"<div><span>Table</span><span>{escape(meta.table_number)}</span></div>")
    if meta.cashier_name:
        parts.append(f"<div><span>Served by</span><span>{escape(meta.cashier_name)}</span></div>")
    if meta.reprint_count:
        parts.append(f"<div><span>Reprinted</span><span>{meta.reprint_count} time(s)</span></div>")
    parts.append("</div>")  # .meta

    parts.append("<hr>")
    for line in document.lines:
        parts.append('<div class="line-item">')
        qty_prefix = f"{line.quantity}x " if line.quantity else ""
        parts.append(
            f'<div class="row"><span>{escape(qty_prefix + line.name)}</span>'
            f"<span>{escape(line.amount_display)}</span></div>"
        )
        for modifier in line.modifiers:
            amt = f" ({escape(modifier.amount_display)})" if modifier.amount_display else ""
            parts.append(f'<div class="modifier"><span>{escape(modifier.label)}{amt}</span><span></span></div>')
        if line.note:
            parts.append(f'<div class="note">Note: {escape(line.note)}</div>')
        parts.append("</div>")

    parts.append("<hr>")
    parts.append('<div class="totals">')
    parts.append(_money_row("Subtotal", totals.subtotal_display))
    if totals.discount_display:
        label = "Discount"
        if totals.discount_reason:
            label = f"Discount ({totals.discount_reason})"
        parts.append(_money_row(label, totals.discount_display))
    if totals.loyalty_discount_display:
        label = "Loyalty reward"
        if totals.loyalty_reward_name:
            label = f"Loyalty reward ({totals.loyalty_reward_name})"
        parts.append(_money_row(label, totals.loyalty_discount_display))
    if totals.delivery_fee_display:
        parts.append(_money_row("Delivery", totals.delivery_fee_display))
    if totals.vat_display:
        if totals.vat_rate_basis_points:
            pct = totals.vat_rate_basis_points / 100
            pct_str = f"{pct:.10g}%"
            label = f"VAT incl. ({pct_str})" if totals.vat_inclusive else f"VAT excl. ({pct_str})"
        else:
            label = "VAT"
        parts.append(_money_row(label, totals.vat_display))
    parts.append(_money_row("Total", totals.total_display, css_class="grand-total"))
    parts.append("</div>")  # .totals

    parts.append('<div class="payment">')
    if payment.method:
        parts.append(_money_row("Payment method", payment.method.replace("_", " ").title()))
    if payment.cash_tendered_display:
        parts.append(_money_row("Cash tendered", payment.cash_tendered_display))
    if payment.change_due_display:
        parts.append(_money_row("Change", payment.change_due_display))
    if payment.refund_amount_display:
        parts.append(_money_row("Refunded", payment.refund_amount_display))
    parts.append("</div>")  # .payment

    if footer.message:
        parts.append(f'<div class="footer">{escape(footer.message)}</div>')

    parts.append('<div class="powered-by">Powered by NextGen Intelligence</div>')

    parts.append("</div>")  # .receipt
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(p for p in parts if p)
