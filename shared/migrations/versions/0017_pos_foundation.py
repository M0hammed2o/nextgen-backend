"""POS foundation: order discount/tax fields, business staff-discount limit, till_sessions

Revision ID: 0017_pos_foundation
Revises: 0016_must_change_password
Create Date: 2026-07-20

Phase 1 of the Restaurant POS module. Adds the columns/table needed for a
POS order-creation endpoint (discount/tax on Order, a per-business staff
discount ceiling) and cash-up reconciliation (till_sessions). Does not touch
any existing table's existing columns.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_pos_foundation"
down_revision = "0016_must_change_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS discount_cents INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS tax_cents INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS discount_reason TEXT NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS discount_approved_by_user_id UUID NULL
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS max_staff_discount_cents INTEGER NOT NULL DEFAULT 0
    """)

    op.create_table(
        "till_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opened_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("closed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opening_float_cents", sa.Integer, nullable=False),
        sa.Column("expected_cash_cents", sa.Integer, nullable=True),
        sa.Column("counted_cash_cents", sa.Integer, nullable=True),
        sa.Column("variance_cents", sa.Integer, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_till_sessions_business_id", "till_sessions", ["business_id"]
    )
    op.create_index(
        "ix_till_sessions_status", "till_sessions", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_till_sessions_status", table_name="till_sessions")
    op.drop_index("ix_till_sessions_business_id", table_name="till_sessions")
    op.drop_table("till_sessions")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS max_staff_discount_cents")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS discount_approved_by_user_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS discount_reason")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS tax_cents")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS discount_cents")
