"""
GET /business/orders/export hardening (Wave 0A P1).

Before this fix:
  (a) customer_name/phone_number/delivery_address/item names — all
      WhatsApp customer free text — were written verbatim into the CSV,
      a classic formula-injection vector when the file is opened in
      Excel/Sheets.
  (b) One OrderItem query PER order, with no cap, for up to 365 days of
      orders.
  (c) No accountant-relevant columns (discount, VAT, payment method/status,
      refund amount) despite them already being on the order.

This proves: injected cells get a defusing leading quote (without
corrupting the numeric currency columns this module formats itself), the
item lookup is a single batched query regardless of order count, an
over-cap export gets a clear 422 instead of running forever, and the new
columns land at the END of the header row (existing positions unmoved).
"""
import csv
import io
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_export import MAX_EXPORT_ORDERS, export_orders_csv
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.order import Order, OrderItem

ORIGINAL_COLUMNS = [
    "Order Number", "Status", "Mode", "Source",
    "Customer Name", "Phone", "Delivery Address",
    "Items", "Subtotal", "Delivery Fee", "Total",
    "Created At", "Accepted At", "Completed At",
]
NEW_COLUMNS = ["Discount", "VAT", "Payment Method", "Payment Status", "Refund Amount"]


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return FakeScalars(self._rows)

    def scalar_one(self):
        return self._scalar


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return self._results.pop(0)


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_order(business_id, **overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status="COMPLETED", order_mode="PICKUP", source="WHATSAPP",
        customer_name="Jane Doe", phone_number="+27821234567", delivery_address=None,
        subtotal_cents=10000, delivery_fee_cents=0, total_cents=10000,
        discount_cents=0, tax_cents=0, payment_method="CASH", payment_status="PAID",
        refund_amount_cents=None, currency="ZAR",
        created_at=now, accepted_at=None, completed_at=now,
    )
    defaults.update(overrides)
    order = Order()
    for k, v in defaults.items():
        setattr(order, k, v)
    return order


def make_item(order_id, business_id, name="Burger", quantity=2):
    item = OrderItem()
    item.id = uuid.uuid4()
    item.order_id = order_id
    item.business_id = business_id
    item.name_snapshot = name
    item.quantity = quantity
    return item


async def _collect_body(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
    return b"".join(chunks)


async def _run(orders, items, user, **kwargs):
    db = FakeDB([
        FakeResult(scalar=len(orders)),  # count query
        FakeResult(rows=orders),          # order query
        FakeResult(rows=items),           # batched item query
    ])
    response = await export_orders_csv(user=user, db=db, **kwargs)
    body = await _collect_body(response)
    rows = list(csv.reader(io.StringIO(body.decode())))
    return rows, db


# ── Formula-injection neutralisation ────────────────────────────────────────

async def test_injection_payloads_are_neutralised():
    user = make_user()
    order = make_order(
        user.business_id,
        customer_name="=cmd|'/c calc'!A1",
        phone_number="+27821234567",
        delivery_address="-1+1;DROP TABLE orders",
    )
    item = make_item(order.id, user.business_id, name="@SUM(A1:A9)")

    rows, _ = await _run([order], [item], user, days=30, status=None)
    header, data_row = rows[0], rows[1]

    name_idx = header.index("Customer Name")
    address_idx = header.index("Delivery Address")
    items_idx = header.index("Items")

    assert data_row[name_idx].startswith("'=")
    assert data_row[address_idx].startswith("'-")
    # The items cell always leads with "{quantity}x " (a digit), so it can
    # never itself start with a formula-trigger character even when an item
    # name does — still exercised through _csv_safe per-item for defense in
    # depth, and asserted here to be a safe (non-formula-leading) cell.
    assert data_row[items_idx][0] not in ("=", "+", "-", "@", "\t", "\r")
    assert "@SUM" in data_row[items_idx]


async def test_tab_and_cr_prefixes_are_neutralised():
    user = make_user()
    order = make_order(user.business_id, customer_name="\tmalicious", delivery_address="\rmalicious")
    rows, _ = await _run([order], [], user, days=30, status=None)
    header, data_row = rows[0], rows[1]
    assert data_row[header.index("Customer Name")].startswith("'\t") or data_row[header.index("Customer Name")].startswith("'")
    assert data_row[header.index("Delivery Address")].startswith("'")


async def test_legitimate_negative_refund_is_not_corrupted():
    """A real negative/refund currency cell must NOT get the defusing quote
    — only free-text customer fields are sanitised."""
    user = make_user()
    order = make_order(user.business_id, refund_amount_cents=500, payment_status="REFUNDED")
    rows, _ = await _run([order], [], user, days=30, status=None)
    header, data_row = rows[0], rows[1]
    refund_cell = data_row[header.index("Refund Amount")]
    assert refund_cell.startswith("R")  # currency symbol, never a defusing quote
    assert not refund_cell.startswith("'")


async def test_normal_customer_name_is_unaffected():
    user = make_user()
    order = make_order(user.business_id, customer_name="Jane Doe")
    rows, _ = await _run([order], [], user, days=30, status=None)
    header, data_row = rows[0], rows[1]
    assert data_row[header.index("Customer Name")] == "Jane Doe"


# ── Batched item query (no N+1) ─────────────────────────────────────────────

async def test_item_lookup_is_a_single_batched_query_for_multiple_orders():
    user = make_user()
    orders = [make_order(user.business_id) for _ in range(5)]
    items = [make_item(o.id, user.business_id) for o in orders]

    rows, db = await _run(orders, items, user, days=30, status=None)

    # count query + order query + exactly ONE item query, regardless of 5 orders
    assert len(db.executed) == 3
    assert len(rows) == 1 + 5  # header + 5 data rows


async def test_no_item_query_when_no_orders():
    user = make_user()
    db = FakeDB([FakeResult(scalar=0), FakeResult(rows=[])])
    response = await export_orders_csv(days=30, status=None, user=user, db=db)
    body = await _collect_body(response)
    rows = list(csv.reader(io.StringIO(body.decode())))
    assert len(rows) == 1  # header only
    assert len(db.executed) == 2  # count + order query, no item query run


# ── Hard cap ─────────────────────────────────────────────────────────────────

async def test_export_over_cap_raises_clear_error():
    user = make_user()
    db = FakeDB([FakeResult(scalar=MAX_EXPORT_ORDERS + 1)])
    with pytest.raises(AppError) as exc:
        await export_orders_csv(days=365, status=None, user=user, db=db)
    assert exc.value.code == "EXPORT_TOO_LARGE"
    assert exc.value.status_code == 422
    assert "narrow" in exc.value.message.lower()
    assert len(db.executed) == 1  # aborted before the order/item queries ran


async def test_export_at_exactly_the_cap_is_allowed():
    user = make_user()
    db = FakeDB([FakeResult(scalar=MAX_EXPORT_ORDERS), FakeResult(rows=[]), ])
    # No orders actually returned here (rows=[]) — only proving the count
    # check itself doesn't reject exactly-at-cap.
    response = await export_orders_csv(days=365, status=None, user=user, db=db)
    assert response.status_code == 200


# ── New accountant columns appended at the end ──────────────────────────────

async def test_new_columns_are_appended_after_existing_ones():
    user = make_user()
    order = make_order(
        user.business_id, discount_cents=500, tax_cents=150,
        payment_method="YOCO", payment_status="PAID", refund_amount_cents=None,
    )
    rows, _ = await _run([order], [], user, days=30, status=None)
    header = rows[0]

    assert header[:len(ORIGINAL_COLUMNS)] == ORIGINAL_COLUMNS
    assert header[len(ORIGINAL_COLUMNS):] == NEW_COLUMNS

    data_row = rows[1]
    assert data_row[header.index("Discount")] == "R5.00"
    assert data_row[header.index("VAT")] == "R1.50"
    assert data_row[header.index("Payment Method")] == "YOCO"
    assert data_row[header.index("Payment Status")] == "PAID"
    assert data_row[header.index("Refund Amount")] == ""
