"""
Digital WhatsApp receipts.

Reuses the SAME two building blocks every other receipt surface uses —
does not introduce a second receipt renderer or a second WhatsApp client:

  - backend/app/receipts/document.py::build_receipt_document() is the one
    structured representation of a slip (trading name, VAT number, lines,
    totals, payment info — all pre-formatted display strings). This module
    only adds a fourth renderer for that same document (JSON-to-PWA,
    ESC/POS, plain text already exist; this is "WhatsApp text").
  - backend/app/bot/whatsapp_sender.py::send_text_message() is the one way
    messages leave this platform on WhatsApp. It persists to the messages
    table and the outbox (reliable delivery + retry) — unlike
    send_notification_message(), which is fire-and-forget with no durable
    record. A receipt is a durable record the customer should reliably
    receive, so it goes through send_text_message(), not
    send_notification_message().

NO PHONE-NUMBER NORMALISATION IS INVENTED HERE
------------------------------------------------
A WhatsApp send requires a wa_id in the exact format Meta gave us on the
inbound webhook (digits only, no leading zero, country code included,
e.g. "27821234567"). Order.phone_number / Customer.phone_number are raw
customer-typed strings (see backend/app/bot/pipeline.py::_parse_name_and_phone,
which stores "0821234567" verbatim) and are NOT reliably in that format.
There is no existing helper anywhere in the bot code that turns a raw typed
phone number into a valid wa_id (verified by repository-wide search this
session), and inventing a new phone-format guesser was explicitly out of
scope for this change. So `send_whatsapp_receipt` below only resolves
`customer.wa_id` — the identity Meta itself gave us — and no-ops (returns
False) when it's absent, rather than guessing. This means POS/manual orders,
whose customers are linked by phone_number only (see
backend/app/loyalty/engine.py::get_or_create_customer_by_phone, which sets
wa_id=None on purpose), are not covered by this change. See TODO.md.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.receipts.document import ReceiptDocument, build_receipt_document

logger = logging.getLogger("nextgen.receipts.whatsapp")


def _payment_method_label(method: str | None) -> str:
    if not method:
        return "—"
    labels = {
        "CASH": "Cash",
        "CASH_ON_COLLECTION": "Cash on collection",
        "CARD": "Card",
        "DIRECT_EFT": "EFT",
        "PAYMENT_LINK": "Online payment",
    }
    return labels.get(method, method.replace("_", " ").title())


def _vat_rate_percent(basis_points: int) -> str:
    pct = basis_points / 100
    # Show a whole number without a trailing ".0", but keep one decimal for
    # a genuinely fractional rate (e.g. 1550 bp -> "15.5%").
    return f"{pct:.10g}%"


def build_whatsapp_receipt_text(document: ReceiptDocument) -> str:
    """
    Render a ReceiptDocument as a WhatsApp text message.

    Reuses the document's own pre-formatted `*_display` strings exclusively
    — never re-derives a money value from a `*_cents` integer, so this can
    never disagree with the printed slip built from the same document.

    WhatsApp text has no reliable fixed-width font, so this is one line per
    item (no column alignment), `*bold*` for the header/total, and no
    table layout.
    """
    lines: list[str] = []

    header = document.header
    title = header.trading_name or "Receipt"
    if header.branch_name:
        title = f"{title} — {header.branch_name}"
    lines.append(f"*{title}*")
    if document.banner:
        lines.append(f"*[{document.banner}]*")

    meta = document.meta
    if meta.order_number:
        lines.append(f"Order #{meta.order_number}")
    if meta.issued_at_local:
        lines.append(meta.issued_at_local)
    lines.append("")

    for item in document.lines:
        qty_prefix = f"{item.quantity}x " if item.quantity else ""
        amount = f" — {item.amount_display}" if item.amount_display else ""
        lines.append(f"{qty_prefix}{item.name}{amount}")
        for modifier in item.modifiers:
            amt = f" ({modifier.amount_display})" if modifier.amount_display else ""
            lines.append(f"   {modifier.label}{amt}")
        if item.note:
            lines.append(f"   Note: {item.note}")
    lines.append("")

    totals = document.totals
    lines.append(f"Subtotal: {totals.subtotal_display}")
    if totals.discount_display:
        reason = f" ({totals.discount_reason})" if totals.discount_reason else ""
        lines.append(f"Discount: {totals.discount_display}{reason}")
    if totals.delivery_fee_display:
        lines.append(f"Delivery: {totals.delivery_fee_display}")
    if totals.vat_display:
        # Only print the rate when the document itself has proven it's still
        # consistent with the stored VAT amount — same rule document.py's
        # _vat_rate_display() already enforces; we just read its output.
        if totals.vat_rate_basis_points:
            rate_str = _vat_rate_percent(totals.vat_rate_basis_points)
            label = "VAT incl." if totals.vat_inclusive else "VAT excl."
            lines.append(f"{label} ({rate_str}): {totals.vat_display}")
        else:
            lines.append(f"VAT: {totals.vat_display}")
    lines.append(f"*Total: {totals.total_display}*")
    lines.append("")

    payment = document.payment
    if payment.method:
        lines.append(f"Payment: {_payment_method_label(payment.method)}")
    if payment.change_due_display:
        lines.append(f"Change: {payment.change_due_display}")

    footer = document.footer
    if footer.message:
        lines.append("")
        lines.append(footer.message)
    lines.append("")
    lines.append("Thank you for your order! 🙏")

    # Collapse the run of blank lines a missing optional section can leave
    # behind, and strip the leading/trailing ones.
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip("\n")


async def send_whatsapp_receipt(
    db: AsyncSession,
    order,
    business,
    customer=None,
) -> bool:
    """
    Build and send the full digital receipt for `order` to its customer.

    Returns True once the send has been handed to send_text_message() (which
    itself persists to the outbox for reliable delivery — an immediate
    transport failure there is still retried later, so True here means
    "queued for reliable delivery", not "Meta confirmed receipt this
    instant"). Returns False, WITHOUT raising, when:
      - business.is_whatsapp_enabled is falsy, or
      - business has no whatsapp_phone_number_id configured, or
      - no customer / no customer.wa_id is resolvable (see module docstring
        — no phone-number is guessed as a fallback).

    Never raises into the caller's transaction — any unexpected error is
    logged and swallowed, matching the fire-and-forget convention already
    used for WhatsApp sends at every _mark_order_paid call site.
    """
    try:
        if not getattr(business, "is_whatsapp_enabled", False):
            return False

        phone_number_id = getattr(business, "whatsapp_phone_number_id", None)
        if not phone_number_id:
            return False

        wa_id = getattr(customer, "wa_id", None) if customer is not None else None
        if not wa_id:
            return False

        document = build_receipt_document(order, business)
        text = build_whatsapp_receipt_text(document)

        from backend.app.bot.whatsapp_sender import send_text_message

        business_id = getattr(business, "id", None) or getattr(order, "business_id", None)
        customer_id = getattr(customer, "id", None)

        await send_text_message(
            db=db,
            business_id=business_id,
            customer_wa_id=wa_id,
            phone_number_id=phone_number_id,
            text=text,
            intent="receipt",
            customer_id=customer_id,
            business=business,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to send WhatsApp receipt for order %s",
            getattr(order, "order_number", "?"),
        )
        return False
