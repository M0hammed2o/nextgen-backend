"""Unique index on orders(business_id, order_number)

Revision ID: 0033_order_number_uniqueness
Revises: 0032_till_one_open_per_business
Create Date: 2026-09-21

Wave 0A. `order_number` (human-friendly, e.g. BO-000123) is only ever meant
to be unique per business — every order-creation path (`routes_pos.py`,
`routes_orders.py::create_order`, the WhatsApp bot pipeline) assigns it via
an atomic `UPDATE businesses SET order_number_sequence = order_number_sequence
+ 1 RETURNING ...` specifically so two concurrent creates can never collide —
but nothing in the schema itself actually enforces that. This migration
closes that gap as a defence-in-depth constraint; it is not expected to
change any application code path under normal operation.

Fails loudly, listing every offending (business_id, order_number) pair and
its order ids, if duplicates already exist — never auto-fixes or deletes
anything.
"""
from alembic import op
import sqlalchemy as sa

revision = "0033_order_number_uniqueness"
down_revision = "0032_till_one_open_per_business"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dup_rows = conn.execute(sa.text("""
        SELECT business_id, order_number, array_agg(id ORDER BY created_at) AS order_ids, count(*) AS cnt
        FROM orders
        GROUP BY business_id, order_number
        HAVING count(*) > 1
    """)).fetchall()
    if dup_rows:
        details = "; ".join(
            f"business_id={r.business_id} order_number={r.order_number!r} "
            f"has {r.cnt} orders: {r.order_ids}"
            for r in dup_rows
        )
        raise RuntimeError(
            "Cannot add uq_orders_business_id_order_number — duplicate "
            "(business_id, order_number) rows already exist and must be "
            "resolved by hand first (this migration does not auto-fix or "
            f"delete anything): {details}"
        )

    op.create_index(
        "uq_orders_business_id_order_number",
        "orders",
        ["business_id", "order_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_orders_business_id_order_number", table_name="orders")
