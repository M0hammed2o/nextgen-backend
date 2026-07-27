"""Payment refund tracking on orders

Revision ID: 0018_payment_refunds
Revises: 0017_pos_foundation
Create Date: 2026-07-22

Adds refund_reference/refund_amount_cents/refunded_at to orders, so the
Payment Provider Framework's refund_payment() has somewhere real to record
its result, distinct from payment_reference (the original charge/payment id).
"""
from alembic import op

revision = "0018_payment_refunds"
down_revision = "0017_pos_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS refund_reference VARCHAR(255) NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS refund_amount_cents INTEGER NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS refunded_at")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS refund_amount_cents")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS refund_reference")
