"""
The single, authoritative "what counts as a sale" definition for every
analytics/reporting endpoint (business summary, daily breakdown, top items,
and the external-orders channel report).

BUG-0043 (see KNOWN_BUGS.md): the summary endpoint used to count revenue and
"completed" orders only for `status IN ('COLLECTED', 'DELIVERED')`. That is
correct ONLY for a WhatsApp pickup/delivery order that is marked complete by
staff working it through the kitchen board. It silently zeroes out:

  - POS till sales, which are created already `payment_status='PAID'` with
    `status` either COMPLETED (grab-and-go, e.g. a drink) or ACCEPTED (needs
    kitchen prep) — see routes_pos.py `create_pos_order()`. A busy
    till-only restaurant would see revenue only once the kitchen board
    happened to also move every order to COLLECTED/DELIVERED, which POS
    sales never do.
  - Imported Uber Eats / Mr D orders, created `status='COMPLETED'`,
    `payment_status='PAID'` — see external_orders/service.py
    `import_resolved_orders()`.

Meanwhile the OLD daily-breakdown endpoint used a different rule entirely
(everything except CANCELLED, with no payment check at all), so a brand new
WhatsApp order sitting unpaid in NEW would already show as "revenue" for the
day it was created. Two endpoints, two different (both wrong) answers to
"is this a sale".

── The definition ────────────────────────────────────────────────────────

A **sale** is any order that is NOT `status == CANCELLED`, and is either:
  - "paid-ish" — `payment_status` in (PAID, PARTIALLY_REFUNDED, REFUNDED); or
  - "fulfilled" — `status` in (COLLECTED, DELIVERED, COMPLETED).

The OR is required because the platform's two order-creation paths reach
"this really happened" from opposite directions:
  - POS/imported-marketplace orders are created already PAID, before/without
    ever necessarily reaching a terminal fulfilment status (a POS order
    needing kitchen prep sits at ACCEPTED, paid, for a while).
  - WhatsApp cash-on-collection orders are the opposite: `payment_status`
    stays `CASH_ON_COLLECTION` (never `PAID`) for the order's entire life —
    see backend/app/bot/pipeline.py — and only the fulfilment status
    (COLLECTED/DELIVERED) tells you money changed hands.

`status == CANCELLED` always excludes an order from being a sale, no matter
its payment_status. This also transparently excludes every POS VOID (see
routes_pos.py `void_pos_order()`, which always sets
`status=CANCELLED, payment_status=VOIDED` together as a single reversal) —
`IS_VOIDED`/`IS_TRUE_CANCELLATION` below split that CANCELLED bucket back
into "reversed till sale" vs. "order that was never fulfilled/paid",
so a manager isn't shown voided till sales lumped in with orders nobody
ever paid for.

Known, accepted gap: an order can be moved straight to CANCELLED via the
generic PATCH /orders/{id}/status transition (routes_orders.py) without
going through routes_pos.py's void/refund endpoints, which do NOT touch
`payment_status`. Such an order would keep `payment_status=PAID` while
`status=CANCELLED`, and — like the pre-existing behaviour of every endpoint
in this file before this fix — is excluded from revenue by this predicate.
This is a real data-integrity gap (money taken with no recorded refund/void)
but is a cancellation-workflow problem, not a reporting-predicate one; fixing
it would mean forcing a refund/void whenever a PAID order is cancelled
outside POS, which is out of scope here. Flagged in KNOWN_BUGS.md BUG-0043.

── Money ──────────────────────────────────────────────────────────────────

  gross_cents  = SUM(total_cents) over sales
  refunds_cents = SUM(COALESCE(refund_amount_cents, 0)) over sales
  net_cents    = gross_cents - refunds_cents
  vat_cents    = SUM(tax_cents) over sales — VAT on GROSS, not pro-rated for
                 partial refunds (the data model has no per-refund VAT split
                 to pro-rate from). Always the stored value, never
                 recomputed from the business's current VAT settings.

A fully refunded order stays a "sale" (so it's visible in order counts) but
nets to 0 revenue. A voided order is excluded from all of the above; report
`voided_orders`/`voided_cents` alongside as a separate, explicit count.

── Timestamps ─────────────────────────────────────────────────────────────

`created_at` is the row's INSERT time. That's correct for POS and WhatsApp
orders (the row is created at the moment of the sale) but WRONG for an
imported Uber Eats/Mr D order: `import_resolved_orders()` never overrides
`TimestampMixin`'s default, so `created_at` there is IMPORT time, which can
be hours or days after the real sale. `external_placed_at` carries the
platform's real order time for those rows and is NULL for every in-store
order, so `COALESCE(external_placed_at, created_at)` gives the true sale
moment uniformly across every source. Every date-range cutoff and every
daily bucket in this module uses that coalesced timestamp, converted to the
business's own local timezone for bucketing (BUG-0032's fix applied
consistently, not just at the range boundary).
"""
from __future__ import annotations

from sqlalchemy import Date, case, cast, func
from sqlalchemy.sql.elements import ColumnElement

from shared.models.order import Order

# ── Predicate vocabulary ────────────────────────────────────────────────────

FULFILLED_STATUSES: tuple[str, ...] = ("COLLECTED", "DELIVERED", "COMPLETED")
PAID_ISH_PAYMENT_STATUSES: tuple[str, ...] = ("PAID", "PARTIALLY_REFUNDED", "REFUNDED")
VOIDED_PAYMENT_STATUS = "VOIDED"
CANCELLED_STATUS = "CANCELLED"


def order_is_sale(status: str, payment_status: str) -> bool:
    """
    Pure-Python mirror of the `IS_SALE` SQL predicate below, kept in lockstep
    deliberately (same tuples, same logic) so it can be unit tested without a
    database and used for any future in-Python filtering.
    """
    if status == CANCELLED_STATUS:
        return False
    return payment_status in PAID_ISH_PAYMENT_STATUSES or status in FULFILLED_STATUSES


def order_is_voided(payment_status: str) -> bool:
    return payment_status == VOIDED_PAYMENT_STATUS


def order_is_true_cancellation(status: str, payment_status: str) -> bool:
    return status == CANCELLED_STATUS and payment_status != VOIDED_PAYMENT_STATUS


# ── SQL predicates ───────────────────────────────────────────────────────────

IS_SALE: ColumnElement = (Order.status != CANCELLED_STATUS) & (
    Order.payment_status.in_(PAID_ISH_PAYMENT_STATUSES) | Order.status.in_(FULFILLED_STATUSES)
)

IS_VOIDED: ColumnElement = Order.payment_status == VOIDED_PAYMENT_STATUS

IS_TRUE_CANCELLATION: ColumnElement = (Order.status == CANCELLED_STATUS) & (
    Order.payment_status != VOIDED_PAYMENT_STATUS
)


# ── Timestamps ───────────────────────────────────────────────────────────────

def sale_timestamp_expr() -> ColumnElement:
    """The timestamp analytics should treat as "when this sale happened"."""
    return func.coalesce(Order.external_placed_at, Order.created_at)


def local_sale_date_expr(tz_name: str):
    """Sale timestamp converted to a business-local calendar DATE, for daily bucketing."""
    return cast(func.timezone(tz_name, sale_timestamp_expr()), Date)


# ── Money / count aggregate expressions (use inside a SQLAlchemy select) ────

def gross_cents_expr() -> ColumnElement:
    return func.coalesce(func.sum(case((IS_SALE, Order.total_cents), else_=0)), 0)


def refunds_cents_expr() -> ColumnElement:
    return func.coalesce(
        func.sum(case((IS_SALE, func.coalesce(Order.refund_amount_cents, 0)), else_=0)), 0
    )


def vat_cents_expr() -> ColumnElement:
    """VAT within gross sales — stored `tax_cents` only, not pro-rated for refunds."""
    return func.coalesce(func.sum(case((IS_SALE, Order.tax_cents), else_=0)), 0)


def sale_count_expr() -> ColumnElement:
    return func.coalesce(func.sum(case((IS_SALE, 1), else_=0)), 0)


def voided_count_expr() -> ColumnElement:
    return func.coalesce(func.sum(case((IS_VOIDED, 1), else_=0)), 0)


def voided_cents_expr() -> ColumnElement:
    return func.coalesce(func.sum(case((IS_VOIDED, Order.total_cents), else_=0)), 0)


def true_cancelled_count_expr() -> ColumnElement:
    return func.coalesce(func.sum(case((IS_TRUE_CANCELLATION, 1), else_=0)), 0)
