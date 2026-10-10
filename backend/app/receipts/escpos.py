"""
ESC/POS rendering for thermal receipt printers.

CAPABILITY DETECTION, NOT BRAND DETECTION
-----------------------------------------
We do not know Ciao Pizza's printer make or model yet, and we deliberately
do not hardcode one. Every optional command sits behind a capability flag on
PrinterCapabilities, so a printer that cannot cut paper, kick a drawer or
print a QR code simply does not receive those bytes — instead of receiving
them and printing garbage characters across the slip, which is the usual
failure when a driver assumes Epson and gets a generic clone.

Defaults are the conservative intersection that virtually every ESC/POS
printer supports: init, alignment, bold, double-height, feed. Cut, drawer
and QR default to ON only because they are near-universal on till printers,
and each can be switched off per device from the print-bridge config once we
see the real hardware.

TEXT LAYOUT
-----------
Slips are monospaced, so layout is character arithmetic: 32 columns at 58 mm,
48 at 80 mm. Every helper here is width-aware and pure, which is why they are
directly unit-testable without a printer attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.app.receipts.document import ReceiptDocument, format_money

# ── Raw ESC/POS control sequences ────────────────────────────────────────────
ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"
ALIGN_LEFT = ESC + b"a\x00"
ALIGN_CENTER = ESC + b"a\x01"
ALIGN_RIGHT = ESC + b"a\x02"
BOLD_ON = ESC + b"E\x01"
BOLD_OFF = ESC + b"E\x00"
DOUBLE_HEIGHT_ON = GS + b"!\x01"
DOUBLE_SIZE_ON = GS + b"!\x11"
SIZE_NORMAL = GS + b"!\x00"
UNDERLINE_ON = ESC + b"-\x01"
UNDERLINE_OFF = ESC + b"-\x00"
LF = b"\n"
# Full cut with a small feed first, so the text clears the cutter blade.
CUT = GS + b"V\x42\x03"
# Drawer kick on pin 2 — the near-universal wiring for a till drawer hung
# off the printer's RJ11/RJ12 port. 50 ms on, 250 ms off.
DRAWER_KICK = ESC + b"p\x00\x32\xfa"


@dataclass
class PrinterCapabilities:
    """
    What a specific physical printer can do.

    Set from the print bridge's per-device config once the real hardware is
    known. Nothing here is brand-specific.
    """

    supports_cut: bool = True
    supports_drawer: bool = True
    supports_qr: bool = True
    supports_bold: bool = True
    supports_double_height: bool = True
    # cp437 is the ESC/POS default and is what unbranded clones assume.
    codepage: str = "cp437"
    # Blank lines fed before the cut so the slip clears the tear bar.
    feed_lines_before_cut: int = 4


def _encode(text: str, capabilities: PrinterCapabilities) -> bytes:
    """
    Encode text for the printer, replacing anything the codepage lacks.

    'replace' rather than raising: a customer name with an unusual character
    must never prevent a receipt from printing at the till.
    """
    return text.encode(capabilities.codepage, errors="replace")


# ── Width-aware text helpers (pure, unit-testable) ───────────────────────────

# format_money() now lives in document.py — the JSON document's *_display
# fields and both text-based renderers must all format money identically,
# so there is exactly one implementation. Re-exported here (rather than
# just imported-and-unused) because existing callers do
# `from backend.app.receipts.escpos import format_money`.
__all__ = ["format_money", "PrinterCapabilities", "render_text", "render_escpos"]


def two_column(left: str, right: str, columns: int) -> str:
    """
    'Subtotal' ......... 'R120.00' across the full width.

    If the pair cannot fit, the LEFT side is truncated — never the right.
    An amount is the one thing on a receipt that must never be clipped.

    The result is always exactly `columns` wide. Reserving the separator
    space BEFORE truncating matters: computing it afterwards produced a
    line one character too long, which on a 32-column roll wraps onto a
    second line and makes the slip look broken.
    """
    right = right or ""
    left = left or ""
    separator = 1 if left else 0
    available = max(0, columns - len(right) - separator)
    left = left[:available]
    gap = max(0, columns - len(left) - len(right))
    return left + " " * gap + right


def centre(text: str, columns: int) -> str:
    text = (text or "")[:columns]
    return text.center(columns).rstrip()


def divider(columns: int, char: str = "-") -> str:
    return char * columns


def wrap(text: str, columns: int, indent: int = 0) -> list[str]:
    """Greedy word wrap; a word longer than the line is hard-split."""
    if not text:
        return []
    prefix = " " * indent
    usable = max(1, columns - indent)
    out: list[str] = []
    current = ""
    for word in str(text).split():
        while len(word) > usable:
            if current:
                out.append(prefix + current)
                current = ""
            out.append(prefix + word[:usable])
            word = word[usable:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= usable:
            current += " " + word
        else:
            out.append(prefix + current)
            current = word
    if current:
        out.append(prefix + current)
    return out


def _qr_command(data: str, capabilities: PrinterCapabilities) -> bytes:
    """
    GS ( k QR sequence: model 2, size 6, error correction M, then store and
    print. Emitted only when the device advertises QR support.
    """
    if not data or not capabilities.supports_qr:
        return b""
    payload = data.encode("utf-8", errors="replace")
    length = len(payload) + 3
    pl, ph = length & 0xFF, (length >> 8) & 0xFF
    return b"".join([
        GS + b"(k\x04\x00\x31\x41\x32\x00",      # model 2
        GS + b"(k\x03\x00\x31\x43\x06",          # module size 6
        GS + b"(k\x03\x00\x31\x45\x31",          # error correction M
        GS + b"(k" + bytes([pl, ph]) + b"\x31\x50\x30" + payload,  # store
        GS + b"(k\x03\x00\x31\x51\x30",          # print
    ])


# ── Plain-text rendering (also the fallback and the debug view) ──────────────

def render_text(document: ReceiptDocument) -> str:
    """
    The slip as plain monospaced text at the document's column width.

    This is the single layout definition — render_escpos() wraps exactly
    these lines in control codes, so the thermal slip and any text/debug
    output can never drift apart.
    """
    cols = document.columns
    out: list[str] = []
    money = lambda c: format_money(c, document.currency)  # noqa: E731

    header = document.header
    # Franchise and trading name are separate fields but frequently hold the
    # same words ("Ciao Pizza"). Printing both would put the name on the slip
    # twice, so only show the franchise line when it actually differs.
    if header.franchise_name and header.franchise_name.strip().casefold() != (
        header.trading_name or ""
    ).strip().casefold():
        out.append(centre(header.franchise_name, cols))
    out.append(centre(header.trading_name, cols))
    if header.branch_name:
        out.append(centre(header.branch_name, cols))
    for line in wrap(header.address, cols):
        out.append(centre(line.strip(), cols))
    if header.phone:
        out.append(centre(f"Tel: {header.phone}", cols))
    if header.vat_number:
        out.append(centre(f"VAT No: {header.vat_number}", cols))
    if header.registration_number:
        out.append(centre(f"Reg No: {header.registration_number}", cols))
    for extra in header.extra_lines:
        out.append(centre(extra, cols))

    if document.document_type == "KITCHEN":
        out.append("")
        out.append(centre("*** KITCHEN ***", cols))
    elif document.footer.show_vat_invoice_label:
        out.append("")
        out.append(centre("TAX INVOICE", cols))

    if document.banner:
        out.append("")
        out.append(centre(f"*** {document.banner} ***", cols))

    out.append(divider(cols, "="))

    meta = document.meta
    out.append(centre(f"Order {meta.order_number}", cols))
    if meta.issued_at:
        stamp = meta.issued_at.replace("T", " ")[:16]
        out.append(centre(stamp, cols))
    if meta.order_type:
        out.append(two_column("Type", meta.order_type, cols))
    if meta.table_number:
        out.append(two_column("Table", meta.table_number, cols))
    if meta.cashier_name:
        out.append(two_column("Cashier", meta.cashier_name, cols))
    if meta.customer_name:
        out.append(two_column("Customer", meta.customer_name, cols))
    out.append(divider(cols))

    is_kitchen = document.document_type == "KITCHEN"
    for line in document.lines:
        label = f"{line.quantity} x {line.name}"
        if is_kitchen:
            out.extend(wrap(label, cols))
        else:
            amount = money(line.amount_cents)
            if len(label) + len(amount) + 1 <= cols:
                out.append(two_column(label, amount, cols))
            else:
                out.extend(wrap(label, cols))
                out.append(two_column("", amount, cols))
        for modifier in line.modifiers:
            text = modifier.label
            if modifier.amount_cents and not is_kitchen:
                out.append(two_column("  " + text, money(modifier.amount_cents), cols))
            else:
                out.extend(wrap(text, cols, indent=2))
        if line.note:
            out.extend(wrap(f"** {line.note}", cols, indent=2))

    if is_kitchen:
        out.append(divider(cols, "="))
        return "\n".join(out)

    out.append(divider(cols))
    totals = document.totals
    out.append(two_column("Subtotal", money(totals.subtotal_cents), cols))
    if totals.discount_cents:
        out.append(two_column("Discount", "-" + money(totals.discount_cents), cols))
        if totals.discount_reason:
            out.extend(wrap(f"  ({totals.discount_reason})", cols))
    if totals.loyalty_discount_cents:
        out.append(two_column("Loyalty reward", "-" + money(totals.loyalty_discount_cents), cols))
        if totals.loyalty_reward_name:
            out.extend(wrap(f"  ({totals.loyalty_reward_name})", cols))
    if totals.delivery_fee_cents:
        out.append(two_column("Delivery", money(totals.delivery_fee_cents), cols))

    if totals.vat_cents:
        # vat_rate_basis_points is None when the tenant's LIVE VAT rate no
        # longer reconciles with this order's own stored VAT amount (see
        # document.py's _vat_rate_display() docstring) — print the amount,
        # which is always correct from the snapshot, without a rate that
        # would contradict it.
        if totals.vat_rate_basis_points:
            rate = Decimal(totals.vat_rate_basis_points) / Decimal(100)
            label = f"VAT @ {rate:.2f}%" + (" (incl)" if totals.vat_inclusive else "")
        else:
            label = "VAT"
        out.append(two_column(label, money(totals.vat_cents), cols))

    out.append(divider(cols))
    out.append(two_column("TOTAL", money(totals.total_cents), cols))
    out.append(divider(cols, "="))

    payment = document.payment
    if payment.allocations:
        # Split payment (migration 0055): one line per settled allocation,
        # each showing its own method label + the portion of the total it
        # settled, with CASH's tendered/change sub-lines indented beneath
        # its own line (never the order-level single-method fields below,
        # which checkout leaves null for a SPLIT order anyway).
        for alloc in payment.allocations:
            out.append(two_column(alloc.method, alloc.amount_display, cols))
            if alloc.cash_tendered_display:
                out.append(two_column("  Cash tendered", alloc.cash_tendered_display, cols))
            if alloc.change_due_display:
                out.append(two_column("  Change", alloc.change_due_display, cols))
    else:
        if payment.method:
            out.append(two_column("Paid by", payment.method, cols))
        if payment.cash_tendered_cents is not None:
            out.append(two_column("Cash", money(payment.cash_tendered_cents), cols))
        if payment.change_due_cents is not None:
            out.append(two_column("Change", money(payment.change_due_cents), cols))
    if payment.refund_amount_cents:
        out.append(two_column("Refunded", money(payment.refund_amount_cents), cols))

    if document.footer.message:
        out.append("")
        for line in wrap(document.footer.message, cols):
            out.append(centre(line.strip(), cols))

    return "\n".join(out)


# ── ESC/POS byte rendering ───────────────────────────────────────────────────

def render_escpos(
    document: ReceiptDocument,
    capabilities: PrinterCapabilities | None = None,
    *,
    open_drawer: bool = False,
) -> bytes:
    """
    The slip as ESC/POS bytes, ready to write to a printer device.

    Layout comes from render_text() so the two renderers cannot diverge;
    this function adds only emphasis, alignment, the optional QR, the cut
    and the optional drawer kick — each gated on a capability flag.
    """
    caps = capabilities or PrinterCapabilities()
    body = render_text(document)
    chunks: list[bytes] = [INIT]

    lines = body.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        is_order_number = stripped.startswith("Order ") and index < 12
        is_total = line.startswith("TOTAL")
        is_banner = stripped.startswith("***") and stripped.endswith("***")

        if is_order_number or is_banner:
            chunks.append(ALIGN_CENTER)
            if caps.supports_double_height:
                chunks.append(DOUBLE_HEIGHT_ON)
            if caps.supports_bold:
                chunks.append(BOLD_ON)
            chunks.append(_encode(stripped, caps) + LF)
            if caps.supports_bold:
                chunks.append(BOLD_OFF)
            chunks.append(SIZE_NORMAL)
            chunks.append(ALIGN_LEFT)
            continue

        if is_total and caps.supports_bold:
            chunks.append(BOLD_ON)
            chunks.append(_encode(line, caps) + LF)
            chunks.append(BOLD_OFF)
            continue

        chunks.append(_encode(line, caps) + LF)

    if document.footer.qr_data and caps.supports_qr:
        chunks.append(ALIGN_CENTER)
        chunks.append(_qr_command(document.footer.qr_data, caps))
        chunks.append(LF)
        chunks.append(ALIGN_LEFT)

    chunks.append(LF * max(0, caps.feed_lines_before_cut))

    if open_drawer and caps.supports_drawer:
        chunks.append(DRAWER_KICK)
    if caps.supports_cut:
        chunks.append(CUT)

    return b"".join(chunks)
