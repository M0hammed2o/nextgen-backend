"""
Platform adapters: an external report's rows -> canonical external orders.

THE POINT OF THIS LAYER
-----------------------
Nothing downstream of here knows what Uber Eats or Mr D call their columns.
An adapter's only job is to turn report rows into ``CanonicalExternalOrder``
objects; the existing recipe and stock-ledger engine does everything after
that.

That separation is what lets a real API integration land later without
touching inventory at all:

    daily spreadsheet ─┐
    Uber Eats API     ─┼─> CanonicalExternalOrder ─> Order ─> Recipe ─> Ledger
    Mr D API          ─┘

These functions are PURE — no database, no SQLAlchemy — so every parsing and
grouping rule below is directly testable without a fixture.

WE DO NOT HAVE THE REAL EXPORTS YET
-----------------------------------
Ciao Pizza's actual Uber Eats and Mr D files have not been seen. So nothing
here hardcodes one layout: headings are matched against alias sets, and
anything unrecognised is surfaced for the manager to map rather than guessed
at. When the real files arrive, the likely change is adding aliases, not
rewriting an adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from shared.enums import ExternalPlatform

# ── Canonical shapes ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalOrderLine:
    """One sold item on an external order, still in the platform's own words."""

    external_item_text: str
    quantity: int
    modifiers: tuple[str, ...] = ()
    size_text: str | None = None
    gross_cents: int | None = None
    source_row: int = 0


@dataclass
class CanonicalExternalOrder:
    """
    One external order, normalised. Still platform vocabulary — mapping to
    this tenant's menu happens later, against saved mappings.
    """

    external_order_id: str
    placed_at: datetime
    lines: list[ExternalOrderLine] = field(default_factory=list)
    status: str | None = None
    gross_cents: int | None = None

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


@dataclass
class RowIssue:
    row: int
    message: str


@dataclass
class AdapterResult:
    orders: list[CanonicalExternalOrder] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    detected_columns: list[str] = field(default_factory=list)
    resolved_mapping: dict[str, str] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return sum(o.item_count for o in self.orders)


# ── Column recognition ───────────────────────────────────────────────────────
#
# Canonical field -> heading aliases we will accept automatically. Matching is
# on a normalised heading (lower-cased, punctuation stripped). A manager can
# always override the result before confirming, so a wrong guess here costs a
# correction, never a bad import.

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "external_order_id": (
        "order id", "orderid", "order number", "order no", "order num", "order",
        "order reference", "reference", "ref", "receipt id", "transaction id",
        "uber order id", "mr d order id", "order code",
    ),
    "order_datetime": (
        "order datetime", "date time", "datetime", "timestamp",
        "ordered at", "placed at", "created at", "order placed",
    ),
    "order_date": ("order date", "date", "day", "business date", "sale date"),
    "order_time": ("order time", "time", "time placed"),
    "item": (
        "item", "item name", "product", "product name", "menu item",
        "dish", "description", "item description", "line item",
    ),
    "quantity": ("quantity", "qty", "count", "units", "item quantity", "no of items"),
    "size": ("size", "variant", "portion size", "item size"),
    "modifiers": (
        "modifier", "modifiers", "options", "option", "add ons", "addons",
        "add-ons", "extras", "customisations", "customizations", "toppings",
        "item options", "modifier list",
    ),
    "price": (
        "price", "gross", "total", "amount", "item total", "line total",
        "gross sales", "item price", "sale amount",
    ),
    # An ORDER-level total repeated on every line of a line-per-row export.
    # Kept apart from `price` (it used to be one of its aliases): summed per
    # line, a R300 three-line order would have been booked as R900.
    "order_total": ("order total", "order amount", "order value", "total order value"),
    "status": ("status", "order status", "state", "order state"),
}

REQUIRED_FIELDS = ("external_order_id", "item", "quantity")

# Statuses that mean the customer never received food. Treated as
# "needs a human decision", never as an automatic non-consumption: a
# cancellation in a financial report says nothing about whether the kitchen
# had already made the pizza.
CANCELLED_STATUS_HINTS = (
    "cancel", "refund", "reject", "fail", "void", "declin", "unfulfil", "unfulfill",
)


def normalise_heading(heading: str) -> str:
    """'Order #  ' -> 'order'.  'Item_Name' -> 'item name'."""
    text = str(heading or "").strip().lower()
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_columns(headings: list[str]) -> dict[str, str]:
    """
    Map canonical field -> the actual heading in this file.

    Exact alias matches win over substring matches, so a file containing both
    "Order ID" and "Order Status" does not resolve the id from the status
    column. First match wins per field; a heading is never claimed twice.
    """
    normalised = {h: normalise_heading(h) for h in headings if h}
    resolved: dict[str, str] = {}
    claimed: set[str] = set()

    for field_name, aliases in COLUMN_ALIASES.items():
        for heading, norm in normalised.items():
            if heading in claimed:
                continue
            if norm in aliases:
                resolved[field_name] = heading
                claimed.add(heading)
                break

    for field_name, aliases in COLUMN_ALIASES.items():
        if field_name in resolved:
            continue
        for heading, norm in normalised.items():
            if heading in claimed or not norm:
                continue
            if any(norm.startswith(a) or a in norm for a in aliases):
                resolved[field_name] = heading
                claimed.add(heading)
                break

    return resolved


# ── Value parsing ────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M",
    "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
)


def parse_datetime(date_text: str, time_text: str | None = None) -> datetime | None:
    """
    Parse a report timestamp, tolerating the formats these exports actually
    use. Returns None rather than guessing — an order with no usable time
    cannot be placed correctly on the stock timeline, so it must be rejected
    rather than silently dated 'now'.

    Naive values are treated as UTC. That is a documented simplification: the
    platform exports we have specified do not carry an offset, and every
    downstream comparison (stocktake cut-offs) is consistent because it uses
    the same convention.
    """
    raw = str(date_text or "").strip()
    if not raw:
        return None
    if time_text and str(time_text).strip() and not re.search(r"\d:\d", raw):
        raw = f"{raw} {str(time_text).strip()}"
    raw = raw.replace("Z", "").strip()
    # Drop a trailing numeric offset such as "+02:00" — see the note above.
    raw = re.sub(r"\s*[+-]\d{2}:?\d{2}$", "", raw).strip()

    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc)

    try:  # last resort: ISO-8601 with anything else in it
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_money_cents(text) -> int | None:
    """'R140.00' / '140,00' / '1,240.50' -> cents. None when absent."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    raw = re.sub(r"[^\d,.\-]", "", raw)
    if not raw or raw in ("-", ".", ","):
        return None
    # "140,00" is decimal-comma; "1,240.50" is a thousands separator.
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif raw.count(",") == 1 and len(raw.split(",")[-1]) in (1, 2):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return int((Decimal(raw) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def parse_quantity(text) -> int | None:
    raw = str(text if text is not None else "").strip()
    if not raw:
        return 1  # a line with no quantity column is one item
    try:
        value = Decimal(re.sub(r"[^\d.\-]", "", raw) or "0")
    except (InvalidOperation, ValueError):
        return None
    if value != value.to_integral_value() or value <= 0:
        return None
    return int(value)


def split_modifiers(text) -> tuple[str, ...]:
    """
    Split a modifier cell into individual modifiers.

    Exports separate these inconsistently — commas, semicolons, pipes and
    newlines all appear — so all are accepted.
    """
    raw = str(text or "").strip()
    if not raw:
        return ()
    parts = re.split(r"[;|\n]+|,(?![^(]*\))", raw)
    return tuple(p.strip() for p in parts if p and p.strip())


def looks_cancelled(status: str | None) -> bool:
    if not status:
        return False
    lowered = str(status).lower()
    return any(hint in lowered for hint in CANCELLED_STATUS_HINTS)


# ── The adapter ──────────────────────────────────────────────────────────────


def adapt_rows(
    rows: list[dict],
    *,
    platform: str = ExternalPlatform.GENERIC.value,
    mapping_override: dict[str, str] | None = None,
) -> AdapterResult:
    """
    Turn report rows into canonical external orders.

    Rows sharing an order id are grouped into one order — every export we
    have specified is one row per LINE ITEM, so a three-item order arrives as
    three rows and must become one order, or duplicate protection would work
    per line instead of per order.

    ``mapping_override`` is the manager's correction of the detected columns,
    as {canonical_field: heading}, applied over auto-detection.
    """
    result = AdapterResult()
    if not rows:
        return result

    headings = []
    for row in rows[:10]:
        for key in row:
            if key and key not in headings:
                headings.append(key)
    result.detected_columns = headings

    resolved = detect_columns(headings)
    for field_name, heading in (mapping_override or {}).items():
        if heading:
            resolved[field_name] = heading
        else:
            resolved.pop(field_name, None)
    result.resolved_mapping = resolved

    result.missing_required = [f for f in REQUIRED_FIELDS if f not in resolved]
    if result.missing_required:
        # Without an order id, an item and a quantity there is nothing to
        # import and no way to deduplicate. Stop rather than half-parse.
        return result

    def value(row: dict, field_name: str):
        heading = resolved.get(field_name)
        return row.get(heading) if heading else None

    grouped: dict[str, CanonicalExternalOrder] = {}
    order_totals: dict[str, int] = {}

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        order_id = str(value(row, "external_order_id") or "").strip()
        item_text = str(value(row, "item") or "").strip()

        if not order_id and not item_text:
            continue  # blank spacer row
        if not order_id:
            result.issues.append(RowIssue(index, "Missing order ID"))
            continue
        if not item_text:
            result.issues.append(RowIssue(index, f"Order {order_id}: missing item name"))
            continue

        quantity = parse_quantity(value(row, "quantity"))
        if quantity is None:
            result.issues.append(
                RowIssue(index, f"Order {order_id}: quantity "
                                f"{value(row, 'quantity')!r} is not a whole number above zero")
            )
            continue

        placed_at = parse_datetime(
            value(row, "order_datetime") or value(row, "order_date"),
            value(row, "order_time"),
        )
        existing = grouped.get(order_id)
        if placed_at is None and existing is None:
            result.issues.append(
                RowIssue(index, f"Order {order_id}: could not read the order date/time. "
                                "Stock has to be dated when the sale happened, so this "
                                "order cannot be imported without it.")
            )
            continue

        status_text = value(row, "status")
        status = str(status_text).strip() if status_text else None
        order_total = parse_money_cents(value(row, "order_total")) if "order_total" in resolved else None

        line = ExternalOrderLine(
            external_item_text=item_text,
            quantity=quantity,
            modifiers=split_modifiers(value(row, "modifiers")),
            size_text=(str(value(row, "size")).strip() or None) if value(row, "size") else None,
            gross_cents=parse_money_cents(value(row, "price")),
            source_row=index,
        )

        if existing is None:
            grouped[order_id] = CanonicalExternalOrder(
                external_order_id=order_id,
                placed_at=placed_at,
                lines=[line],
                status=status,
                gross_cents=line.gross_cents,
            )
            if order_total is not None:
                order_totals[order_id] = order_total
        else:
            existing.lines.append(line)
            if existing.status is None and status:
                existing.status = status
            # Earliest timestamp on the order wins — later lines in an export
            # sometimes carry a fulfilment time rather than the order time.
            if placed_at and placed_at < existing.placed_at:
                existing.placed_at = placed_at
            if line.gross_cents is not None:
                existing.gross_cents = (existing.gross_cents or 0) + line.gross_cents
            if order_total is not None and order_id not in order_totals:
                order_totals[order_id] = order_total

    # An explicit order-level total is the order's value, taken once — never
    # summed across its lines (the lines still carry their own prices).
    for order_id, total in order_totals.items():
        grouped[order_id].gross_cents = total

    result.orders = list(grouped.values())
    return result


def order_id_prefix(platform: str) -> str:
    """Short human prefix for a generated NextGen order number."""
    return {
        ExternalPlatform.UBER_EATS.value: "UE",
        ExternalPlatform.MR_D.value: "MRD",
    }.get(platform, "EXT")
