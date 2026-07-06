"""Add payment_method, payment_reference, paid_at to orders

Revision ID: 0007_add_payment_fields
Revises: 0006_phase8_pricing_engine
Create Date: 2026-07-06

Changes:
  - Adds payment_method VARCHAR(16) NULL (CASH | CARD)
  - Adds payment_reference VARCHAR(255) NULL (card/EFT reference)
  - Adds paid_at TIMESTAMPTZ NULL (when payment_status became PAID)

All columns are nullable so existing rows remain valid.
Uses IF NOT EXISTS / DO NOTHING guards for safe re-running.
"""
from alembic import op


revision = "0007_add_payment_fields"
down_revision = "0006_phase8_pricing_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_method   VARCHAR(16),
            ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(255),
            ADD COLUMN IF NOT EXISTS paid_at          TIMESTAMPTZ;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            DROP COLUMN IF EXISTS payment_method,
            DROP COLUMN IF EXISTS payment_reference,
            DROP COLUMN IF EXISTS paid_at;
    """)
