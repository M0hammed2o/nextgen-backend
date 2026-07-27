"""Loyalty Programme: program config, reward catalogue, points ledger

Revision ID: 0019_loyalty_programme
Revises: 0018_payment_refunds
Create Date: 2026-07-24

Adds loyalty_programs (one row per business, opt-in, is_enabled defaults
false), loyalty_rewards (per-business reward catalogue), and
loyalty_transactions (append-only points ledger). Also relaxes
customers.wa_id to nullable and adds loyalty fields to customers, so a
POS-only (non-WhatsApp) customer can be enrolled in loyalty.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019_loyalty_programme"
down_revision = "0018_payment_refunds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE customers ALTER COLUMN wa_id DROP NOT NULL")
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS loyalty_points_balance INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS loyalty_tier VARCHAR(16) NOT NULL DEFAULT 'BRONZE'
    """)
    op.execute("""
        ALTER TABLE customers
            ADD COLUMN IF NOT EXISTS loyalty_points_expire_at TIMESTAMPTZ NULL
    """)

    op.create_table(
        "loyalty_programs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("points_per_rand", sa.Integer, nullable=False, server_default="1"),
        sa.Column("points_per_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("points_expiry_days", sa.Integer, nullable=True),
        sa.Column("min_redemption_points", sa.Integer, nullable=False, server_default="0"),
        sa.Column("silver_tier_points", sa.Integer, nullable=False, server_default="500"),
        sa.Column("gold_tier_points", sa.Integer, nullable=False, server_default="2000"),
        sa.Column("birthday_reward_points", sa.Integer, nullable=True),
        sa.Column("referral_reward_points", sa.Integer, nullable=True),
        sa.Column("double_points_promo_active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("double_points_promo_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("double_points_promo_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_loyalty_programs_business_id", "loyalty_programs", ["business_id"])

    op.create_table(
        "loyalty_rewards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("points_cost", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_loyalty_rewards_business_id", "loyalty_rewards", ["business_id"])

    op.create_table(
        "loyalty_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transaction_type", sa.String(16), nullable=False),
        sa.Column("points_delta", sa.Integer, nullable=False),
        sa.Column("balance_after", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_loyalty_transactions_business_id", "loyalty_transactions", ["business_id"])
    op.create_index("ix_loyalty_transactions_customer_id", "loyalty_transactions", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_loyalty_transactions_customer_id", table_name="loyalty_transactions")
    op.drop_index("ix_loyalty_transactions_business_id", table_name="loyalty_transactions")
    op.drop_table("loyalty_transactions")
    op.drop_index("ix_loyalty_rewards_business_id", table_name="loyalty_rewards")
    op.drop_table("loyalty_rewards")
    op.drop_index("ix_loyalty_programs_business_id", table_name="loyalty_programs")
    op.drop_table("loyalty_programs")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS loyalty_points_expire_at")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS loyalty_tier")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS loyalty_points_balance")
    # Not reverting wa_id to NOT NULL — any POS-only customer rows created in
    # the meantime would violate that constraint; a downgrade this deep
    # requires a manual data decision, not a blind schema revert.
