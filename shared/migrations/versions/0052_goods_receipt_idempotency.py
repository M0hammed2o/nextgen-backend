"""Goods receipt idempotency (BUG-0112)

Revision ID: 0052_goods_receipt_idempotency
Revises: 0051_loyalty_entitlement
Create Date: 2026-10-05

Closes a real gap: `goods_receipts` had no idempotency mechanism at all,
unlike `stock_movements` (`uq_stock_movements_business_id_idempotency_key`,
migration 0025) or `orders` (`uq_orders_business_idempotency_key`, migration
0023). A double-click, a client retry after an apparent timeout, or two tabs
submitting the same delivery each created a fully independent GoodsReceipt —
silently doubling both the recorded quantity and cost.

Adds a nullable `idempotency_key` column (old clients that send no key keep
working exactly as before — same compatibility shape as
`orders.idempotency_key`) plus a partial unique index on
`(business_id, idempotency_key)`, excluding NULLs, mirroring
`uq_orders_business_idempotency_key` exactly (a plain UniqueConstraint can't
express the "only when NOT NULL" condition, and the naming-convention-derived
name would collide on column_0 alone across multiple two-column unique
constraints — the same reason 0023 used an explicit named partial index
instead of a declared UniqueConstraint on the model).
"""
from alembic import op
import sqlalchemy as sa

revision = "0052_goods_receipt_idempotency"
down_revision = "0051_loyalty_entitlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE goods_receipts
            ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) NULL
    """)
    # Partial: only rows that actually carry a key participate, so the many
    # existing/legacy receipts with no key (and any future receipt from a
    # client that doesn't send one) never collide with each other.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_goods_receipts_business_idempotency_key
            ON goods_receipts (business_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_goods_receipts_business_idempotency_key")
    op.execute("ALTER TABLE goods_receipts DROP COLUMN IF EXISTS idempotency_key")
