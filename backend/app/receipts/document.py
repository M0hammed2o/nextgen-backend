"""
The receipt document: one structured representation of a till slip, rendered
by several different back-ends.

WHY A DOCUMENT AND NOT AN IMAGE
-------------------------------
The client will hand us a photo or PDF of their current Ciao Pizza slip. We
deliberately do NOT print that file. A thermal head is 1-bit and either 384
dots (58 mm) or 576 dots (80 mm) wide; a photo prints slowly and badly,
cannot reflow for a variable number of line items, and would freeze the
order number, totals and VAT as pixels. A South African tax invoice also has
content requirements a picture cannot satisfy dynamically.

So the uploaded slip is stored as an onboarding *design reference*
(Business.receipt_reference_asset_id) and reproduced here as a structured
template driven by Business.receipt_config_json.

ONE DOCUMENT, THREE RENDERERS
-----------------------------
    build_receipt_document()  ->  ReceiptDocument
                                     |
              +----------------------+----------------------+
              |                      |                      |
        JSON to the PWA        ESC/POS bytes           plain text
        (browser/kiosk          (print bridge)        (fallback/debug)
         printing)

Keeping content in one place is what stops the browser slip and the thermal
slip drifting apart — the classic failure mode when each is hand-built. This
also means every money value on the document is already formatted once here
(the `*_display` fields, alongside the raw `*_cents` integers) so a frontend
consuming the JSON never has to re-do currency arithmetic or reformat a cents
integer itself — it can lay the slip out from strings and structure alone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal

from shared.pricing.engine import calculate_vat_cents
from shared.utils.time import to_business_tz

# Character columns available at each supported roll width, in the standard
# Font A. These are the ESC/POS conventions virtually every 58/80 mm printer
# follows, and they also drive the browser CSS width.
COLUMNS_FOR_WIDTH: dict[int, int] = {58: 32, 80: 48}
DEFAULT_PAPER_WIDTH_MM = 80
DEFAULT_TIMEZONE = "Africa/Johannesburg"


def columns_for(paper_width_mm: int) -> int:
    return COLUMNS_FOR_WIDTH.get(int(paper_width_mm or 0), COLUMNS_FOR_WIDTH[DEFAULT_PAPER_WIDTH_MM])


def format_money(cents: int, currency: str = "ZAR") -> str:
    """
    The one money-formatting function every renderer (JSON display fields,
    plain text, ESC/POS) goes through, so R120.00 is never spelled two
    different ways on the same slip.
    """
    symbol = "R" if currency == "ZAR" else ""
    negative = cents < 0
    value = Decimal(abs(int(cents or 0))) / Decimal(100)
    return f"{'-' if negative else ''}{symbol}{value:.2f}"


@dataclass
class ReceiptLineModifier:
    """An option, add-on or removal shown under a line item."""

    label: str
    amount_cents: int | None = None
    amount_display: str | None = None


@dataclass
class ReceiptLine:
    quantity: int
    name: str
    unit_amount_cents: int = 0
    unit_amount_display: str = ""
    amount_cents: int = 0
    amount_display: str = ""
    modifiers: list[ReceiptLineModifier] = field(default_factory=list)
    note: str | None = None


@dataclass
class ReceiptTotals:
    subtotal_cents: int = 0
    subtotal_display: str = ""
    discount_cents: int = 0
    discount_display: str | None = None
    discount_reason: str | None = None
    # Loyalty reward redemption — a distinct line from the staff/manual
    # discount above, added migration 0050. loyalty_discount_cents is the
    # CLAMPED amount actually applied (see LoyaltyReward.value_cents'
    # clamp-and-warn behaviour); loyalty_points_redeemed is always the
    # reward's full points cost, even when the discount was clamped.
    loyalty_discount_cents: int = 0
    loyalty_discount_display: str | None = None
    loyalty_reward_name: str | None = None
    loyalty_points_redeemed: int = 0
    delivery_fee_cents: int = 0
    delivery_fee_display: str | None = None
    vat_cents: int = 0
    vat_display: str | None = None
    # None (rather than 0/False) means "not shown" — see
    # _vat_rate_display()'s docstring for why a rate is only printed when it
    # can be proven consistent with the stored vat_cents.
    vat_rate_basis_points: int | None = None
    vat_inclusive: bool | None = None
    total_cents: int = 0
    total_display: str = ""


@dataclass
class ReceiptPayment:
    method: str | None = None
    status: str | None = None
    cash_tendered_cents: int | None = None
    cash_tendered_display: str | None = None
    change_due_cents: int | None = None
    change_due_display: str | None = None
    reference: str | None = None
    refund_amount_cents: int | None = None
    refund_amount_display: str | None = None


@dataclass
class ReceiptHeader:
    trading_name: str = ""
    franchise_name: str | None = None
    branch_name: str | None = None
    address: str | None = None
    phone: str | None = None
    vat_number: str | None = None
    registration_number: str | None = None
    logo_url: str | None = None
    extra_lines: list[str] = field(default_factory=list)


@dataclass
class ReceiptMeta:
    order_number: str = ""
    issued_at: str | None = None
    # Same instant as issued_at, formatted in the business's own timezone —
    # the frontend must never localise this itself, since it does not know
    # (and must not be trusted to know) the tenant's timezone.
    issued_at_local: str | None = None
    cashier_name: str | None = None
    order_type: str | None = None
    table_number: str | None = None
    customer_name: str | None = None
    till_session_reference: str | None = None
    # 0 for an original print. >0 counts how many times this order's receipt
    # has been reprinted before (not counting the copy currently returned).
    reprint_count: int = 0
    last_reprinted_at: str | None = None


@dataclass
class ReceiptFooter:
    message: str | None = None
    qr_data: str | None = None
    show_vat_invoice_label: bool = False


@dataclass
class ReceiptDocument:
    """Everything needed to print one slip, renderer-agnostic."""

    document_type: str = "CUSTOMER_RECEIPT"  # or KITCHEN
    paper_width_mm: int = DEFAULT_PAPER_WIDTH_MM
    columns: int = COLUMNS_FOR_WIDTH[DEFAULT_PAPER_WIDTH_MM]
    currency: str = "ZAR"
    copies: int = 1

    header: ReceiptHeader = field(default_factory=ReceiptHeader)
    meta: ReceiptMeta = field(default_factory=ReceiptMeta)
    lines: list[ReceiptLine] = field(default_factory=list)
    totals: ReceiptTotals = field(default_factory=ReceiptTotals)
    payment: ReceiptPayment = field(default_factory=ReceiptPayment)
    footer: ReceiptFooter = field(default_factory=ReceiptFooter)

    # Banner text for an abnormal state — REPRINT, REFUNDED, SALE VOIDED.
    # A reprint must be visibly distinguishable from an original, otherwise
    # a receipt stops being evidence of anything.
    banner: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Building from domain objects ─────────────────────────────────────────────

def _config(business) -> dict:
    return getattr(business, "receipt_config_json", None) or {}


def _order_type_label(order) -> str | None:
    mode = getattr(order, "order_mode", None)
    return {
        "DINE_IN": "Dine In",
        "PICKUP": "Takeaway",
        "DELIVERY": "Delivery",
    }.get(mode, mode)


def _localise(issued_at: datetime | None, business) -> str | None:
    """
    Format the order's UTC timestamp in the tenant's own timezone.

    Falls back to the raw UTC stamp if the business's timezone string is
    missing or invalid, rather than raising — a bad/unset timezone value
    must never prevent a receipt from printing.
    """
    if not isinstance(issued_at, datetime):
        return None
    tz_name = getattr(business, "timezone", None) or DEFAULT_TIMEZONE
    try:
        local = to_business_tz(issued_at, tz_name)
    except Exception:
        local = issued_at
    return local.strftime("%Y-%m-%d %H:%M")


def _vat_rate_display(order, business) -> tuple[int | None, bool | None]:
    """
    Decide whether the tenant's LIVE VAT rate/inclusive setting can be
    printed on this receipt at all.

    KNOWN LIMITATION (see KNOWN_BUGS BUG-0045 follow-up): Business.
    vat_rate_basis_points and vat_inclusive_pricing are live columns, not a
    snapshot taken on the order at checkout — there is no column recording
    what rate actually applied when this order was placed. Order.tax_cents
    itself IS a snapshot and is always historically correct on its own.

    So: reconstruct what the VAT amount would be if today's live rate had
    applied to this order's own stored net amount (subtotal - discount -
    loyalty_discount + delivery fee — the same base routes_pos.py used to
    compute tax_cents at checkout, migration 0050 added loyalty_discount_cents
    to that base). If that reconstruction matches the stored tax_cents exactly,
    the live rate still explains this receipt and is safe to print. If the
    tenant has since changed its VAT rate or inclusive/exclusive setting,
    the numbers will disagree — in that case we print the VAT amount (it is
    always correct, from the snapshot) but omit the rate/inclusive label
    rather than print a rate that contradicts the amount next to it. A tax
    invoice that states an amount and a rate that don't reconcile is worse
    than one that simply omits the rate.

    Follow-up (not done here — would need a migration, out of scope for this
    change): snapshot vat_rate_basis_points/vat_inclusive_pricing onto the
    Order at checkout so the true historical rate can always be shown.
    """
    tax_cents = int(getattr(order, "tax_cents", 0) or 0)
    if tax_cents <= 0:
        return None, None

    rate = int(getattr(business, "vat_rate_basis_points", 0) or 0)
    if rate <= 0:
        return None, None
    inclusive = bool(getattr(business, "vat_inclusive_pricing", True))

    net_total_cents = (
        int(getattr(order, "subtotal_cents", 0) or 0)
        - int(getattr(order, "discount_cents", 0) or 0)
        - int(getattr(order, "loyalty_discount_cents", 0) or 0)
        + int(getattr(order, "delivery_fee_cents", 0) or 0)
    )
    expected = calculate_vat_cents(net_total_cents, rate, inclusive)
    if expected != tax_cents:
        return None, None
    return rate, inclusive


def build_receipt_document(
    order,
    business,
    *,
    order_items=None,
    document_type: str = "CUSTOMER_RECEIPT",
    cashier_name: str | None = None,
    is_reprint: bool = False,
    reprint_count: int = 0,
    last_reprinted_at: str | None = None,
) -> ReceiptDocument:
    """
    Assemble a printable document from an Order plus its business config.

    Reads exclusively from the order's own SNAPSHOT fields (name_snapshot,
    selected_options_snapshot, add_ons_snapshot, removed_ingredients_snapshot,
    unit_price_cents, line_total_cents, discount_cents, discount_reason,
    tax_cents, total_cents, cash_tendered_cents, change_due_cents,
    refund_amount_cents) rather than from the live menu or a recomputation,
    so reprinting a month-old receipt shows what was actually sold and
    charged, not what the menu or business settings say today. The one
    exception, documented in _vat_rate_display()'s docstring, is the VAT
    rate/inclusive label — Business has no snapshot column for it, so it is
    only shown when it still reconciles with the stored tax_cents.

    `reprint_count`/`last_reprinted_at` are supplied by the caller (route
    layer), which is the only place with access to the audit trail.
    """
    config = _config(business)
    currency = getattr(order, "currency", None) or getattr(business, "currency", "ZAR")
    money = lambda c: format_money(c, currency)  # noqa: E731
    width = int(getattr(business, "receipt_paper_width_mm", DEFAULT_PAPER_WIDTH_MM) or DEFAULT_PAPER_WIDTH_MM)
    items = order_items if order_items is not None else (getattr(order, "items", None) or [])
    is_kitchen = document_type == "KITCHEN"

    header = ReceiptHeader(
        trading_name=config.get("trading_name") or getattr(business, "name", "") or "",
        franchise_name=config.get("franchise_name"),
        branch_name=config.get("branch_name"),
        address=config.get("address") or getattr(business, "address", None),
        phone=config.get("phone") or getattr(business, "phone", None),
        vat_number=(
            getattr(business, "vat_number", None)
            if getattr(business, "vat_registered", False)
            else None
        ),
        registration_number=config.get("registration_number"),
        logo_url=config.get("logo_url"),
        extra_lines=list(config.get("header_lines") or []),
    )

    lines: list[ReceiptLine] = []
    for item in items:
        modifiers: list[ReceiptLineModifier] = []
        for option in getattr(item, "selected_options_snapshot", None) or []:
            if isinstance(option, dict) and option.get("option_name"):
                delta = option.get("price_delta_cents") or 0
                modifiers.append(
                    ReceiptLineModifier(
                        label=str(option["option_name"]),
                        amount_cents=delta if delta else None,
                        amount_display=money(delta) if delta else None,
                    )
                )
        for add_on in getattr(item, "add_ons_snapshot", None) or []:
            if isinstance(add_on, dict) and add_on.get("name"):
                qty = int(add_on.get("quantity", 1) or 1)
                label = f"+ {add_on['name']}" + (f" x{qty}" if qty > 1 else "")
                total = int(add_on.get("price_cents", 0) or 0) * qty
                modifiers.append(
                    ReceiptLineModifier(
                        label=label,
                        amount_cents=total or None,
                        amount_display=money(total) if total else None,
                    )
                )
        for removed in getattr(item, "removed_ingredients_snapshot", None) or []:
            if isinstance(removed, dict) and removed.get("name"):
                modifiers.append(ReceiptLineModifier(label=f"No {removed['name']}"))

        unit_amount_cents = 0 if is_kitchen else int(getattr(item, "unit_price_cents", 0) or 0)
        amount_cents = 0 if is_kitchen else int(getattr(item, "line_total_cents", 0) or 0)
        lines.append(
            ReceiptLine(
                quantity=int(getattr(item, "quantity", 1) or 1),
                name=str(getattr(item, "name_snapshot", "") or ""),
                # A kitchen slip never shows prices — it is a prep instruction.
                unit_amount_cents=unit_amount_cents,
                unit_amount_display=money(unit_amount_cents) if not is_kitchen else "",
                amount_cents=amount_cents,
                amount_display=money(amount_cents) if not is_kitchen else "",
                modifiers=modifiers,
                note=getattr(item, "special_instructions", None),
            )
        )

    subtotal_cents = int(getattr(order, "subtotal_cents", 0) or 0)
    discount_cents = int(getattr(order, "discount_cents", 0) or 0)
    loyalty_discount_cents = int(getattr(order, "loyalty_discount_cents", 0) or 0)
    delivery_fee_cents = int(getattr(order, "delivery_fee_cents", 0) or 0)
    vat_cents = int(getattr(order, "tax_cents", 0) or 0)
    total_cents = int(getattr(order, "total_cents", 0) or 0)
    vat_rate_basis_points, vat_inclusive = _vat_rate_display(order, business)

    totals = ReceiptTotals(
        subtotal_cents=subtotal_cents,
        subtotal_display=money(subtotal_cents),
        discount_cents=discount_cents,
        discount_display=("-" + money(discount_cents)) if discount_cents else None,
        discount_reason=getattr(order, "discount_reason", None),
        loyalty_discount_cents=loyalty_discount_cents,
        loyalty_discount_display=("-" + money(loyalty_discount_cents)) if loyalty_discount_cents else None,
        loyalty_reward_name=getattr(order, "loyalty_reward_name", None),
        loyalty_points_redeemed=int(getattr(order, "loyalty_points_redeemed", 0) or 0),
        delivery_fee_cents=delivery_fee_cents,
        delivery_fee_display=money(delivery_fee_cents) if delivery_fee_cents else None,
        vat_cents=vat_cents,
        vat_display=money(vat_cents) if vat_cents else None,
        vat_rate_basis_points=vat_rate_basis_points,
        vat_inclusive=vat_inclusive,
        total_cents=total_cents,
        total_display=money(total_cents),
    )

    payment_status = getattr(order, "payment_status", None)
    cash_tendered = getattr(order, "cash_tendered_cents", None)
    change_due = getattr(order, "change_due_cents", None)
    refund_amount = getattr(order, "refund_amount_cents", None)
    payment = ReceiptPayment(
        method=getattr(order, "payment_method", None),
        status=payment_status,
        cash_tendered_cents=cash_tendered,
        cash_tendered_display=money(cash_tendered) if cash_tendered is not None else None,
        change_due_cents=change_due,
        change_due_display=money(change_due) if change_due is not None else None,
        reference=getattr(order, "payment_reference", None),
        refund_amount_cents=refund_amount,
        refund_amount_display=money(refund_amount) if refund_amount else None,
    )

    issued = getattr(order, "created_at", None)
    meta = ReceiptMeta(
        order_number=str(getattr(order, "order_number", "") or ""),
        issued_at=issued.isoformat() if isinstance(issued, datetime) else None,
        issued_at_local=_localise(issued, business),
        cashier_name=cashier_name,
        order_type=_order_type_label(order),
        table_number=getattr(order, "table_number", None),
        customer_name=getattr(order, "customer_name", None),
        reprint_count=int(reprint_count or 0),
        last_reprinted_at=last_reprinted_at,
    )

    # Abnormal-state banner. A voided or refunded slip, and any reprint, must
    # be visibly different from an original.
    banner = None
    if payment_status == "VOIDED":
        banner = "SALE VOIDED"
    elif payment_status in ("REFUNDED", "PARTIALLY_REFUNDED"):
        banner = "REFUNDED"
    elif is_reprint:
        banner = "REPRINT"

    footer = ReceiptFooter(
        message=config.get("footer_message"),
        qr_data=config.get("qr_data"),
        show_vat_invoice_label=bool(getattr(business, "vat_registered", False)),
    )

    return ReceiptDocument(
        document_type=document_type,
        paper_width_mm=width,
        columns=columns_for(width),
        currency=currency,
        copies=int(getattr(business, "receipt_copies", 1) or 1),
        header=header,
        meta=meta,
        lines=lines,
        totals=totals,
        payment=ReceiptPayment() if is_kitchen else payment,
        footer=ReceiptFooter() if is_kitchen else footer,
        banner=banner,
    )
