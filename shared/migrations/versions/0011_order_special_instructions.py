"""Add special_instructions column to orders

Revision ID: 0011_order_notes
Revises: 0010_push_subs
Create Date: 2026-07-08
"""
from alembic import op

revision = "0011_order_notes"
down_revision = "0010_push_subs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS special_instructions TEXT
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            DROP COLUMN IF EXISTS special_instructions
    """)
