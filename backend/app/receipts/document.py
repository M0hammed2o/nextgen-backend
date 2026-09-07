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
slip drifting apart — the classic failure mode when each is hand-built.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

# Character columns available at each supported roll width, in the standard
# Font A. These are the ESC/POS conventions virtually every 58/80 mm printer
# follows, and they also drive the browser CSS width.
COLUMNS_FOR_WIDTH: dict[int, int] = {58: 32, 80: 48}
DEFAULT_PAPER_WIDTH_MM = 80


def columns_for(paper_width_mm: int) -> int:
    return COLUMNS_FOR_WIDTH.get(int(paper_width_mm or 0), COLUMNS_FOR_WIDTH[DEFAULT_PAPER_WIDTH_MM])


@dataclass
class ReceiptLineModifier:
    """An option, add-on or removal shown under a line item."""

    label: str
    amount_cents: int | None = None


@dataclass
class ReceiptLine:
    quantity: int
    name: str
    amount_cents: int
    modifiers: list[ReceiptLineModifier] = field(default_factory=list)
    note: str | None = None


@dataclass
class ReceiptTotals:
    subtotal_cents: int = 0
    discount_cents: int = 0
    delivery_fee_cents: int = 0
    vat_cents: int = 0
    vat_rate_basis_points: int = 0
    vat_inclusive: bool = True
    total_cents: int = 0


@dataclass
class ReceiptPayment:
    method: str | None = None
    status: str | None = None
    cash_tendered_cents: int | None = None
    change_due_cents: int | None = None
    reference: str | None = None
    refund_amount_cents: int | None = None


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
    cashier_name: str | None = None
    order_type: str | None = None
    table_number: str | None = None
    customer_name: str | None = None
    till_session_reference: str | None = None


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


def build_receipt_document(
    order,
    business,
    *,
    order_items=None,
    document_type: str = "CUSTOMER_RECEIPT",
    cashier_name: str | None = None,
    is_reprint: bool = False,
) -> ReceiptDocument:
    """
    Assemble a printable document from an Order plus its business config.

    Reads exclusively from the order's own SNAPSHOT fields (name_snapshot,
    selected_options_snapshot, add_ons_snapshot, removed_ingredients_snapshot)
    rather than from the live menu, so reprinting a month-old receipt shows
    what was actually sold and charged, not what the menu says today.
    """
    config = _config(business)
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
                    )
                )
        for add_on in getattr(item, "add_ons_snapshot", None) or []:
            if isinstance(add_on, dict) and add_on.get("name"):
                qty = int(add_on.get("quantity", 1) or 1)
                label = f"+ {add_on['name']}" + (f" x{qty}" if qty > 1 else "")
                total = int(add_on.get("price_cents", 0) or 0) * qty
                modifiers.append(ReceiptLineModifier(label=label, amount_cents=total or None))
        for removed in getattr(item, "removed_ingredients_snapshot", None) or []:
            if isinstance(removed, dict) and removed.get("name"):
                modifiers.append(ReceiptLineModifier(label=f"No {removed['name']}"))

        lines.append(
            ReceiptLine(
                quantity=int(getattr(item, "quantity", 1) or 1),
                name=str(getattr(item, "name_snapshot", "") or ""),
                # A kitchen slip never shows prices — it is a prep instruction.
                amount_cents=0 if is_kitchen else int(getattr(item, "line_total_cents", 0) or 0),
                modifiers=modifiers,
                note=getattr(item, "special_instructions", None),
            )
        )

    totals = ReceiptTotals(
        subtotal_cents=int(getattr(order, "subtotal_cents", 0) or 0),
        discount_cents=int(getattr(order, "discount_cents", 0) or 0),
        delivery_fee_cents=int(getattr(order, "delivery_fee_cents", 0) or 0),
        vat_cents=int(getattr(order, "tax_cents", 0) or 0),
        vat_rate_basis_points=int(getattr(business, "vat_rate_basis_points", 0) or 0),
        vat_inclusive=bool(getattr(business, "vat_inclusive_pricing", True)),
        total_cents=int(getattr(order, "total_cents", 0) or 0),
    )

    payment_status = getattr(order, "payment_status", None)
    payment = ReceiptPayment(
        method=getattr(order, "payment_method", None),
        status=payment_status,
        cash_tendered_cents=getattr(order, "cash_tendered_cents", None),
        change_due_cents=getattr(order, "change_due_cents", None),
        reference=getattr(order, "payment_reference", None),
        refund_amount_cents=getattr(order, "refund_amount_cents", None),
    )

    issued = getattr(order, "created_at", None)
    meta = ReceiptMeta(
        order_number=str(getattr(order, "order_number", "") or ""),
        issued_at=issued.isoformat() if isinstance(issued, datetime) else None,
        cashier_name=cashier_name,
        order_type=_order_type_label(order),
        table_number=getattr(order, "table_number", None),
        customer_name=getattr(order, "customer_name", None),
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
        currency=getattr(order, "currency", None) or getattr(business, "currency", "ZAR"),
        copies=int(getattr(business, "receipt_copies", 1) or 1),
        header=header,
        meta=meta,
        lines=lines,
        totals=totals,
        payment=ReceiptPayment() if is_kitchen else payment,
        footer=ReceiptFooter() if is_kitchen else footer,
        banner=banner,
    )
