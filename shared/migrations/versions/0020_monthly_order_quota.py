"""Monthly order quota / order-pack credits for the NextGen Restaurant plan

Revision ID: 0020_monthly_order_quota
Revises: 0019_loyalty_programme
Create Date: 2026-07-24

Adds businesses.monthly_order_quota/order_pack_credits/billing_period_started_at
— the "1,500 Included Customer Orders per Month" + Order Pack add-on pricing
model. Distinct from the pre-existing daily_order_limit (a same-day
runaway-cost guard, unrelated to this billing-period quota).
"""
from alembic import op

revision = "0020_monthly_order_quota"
down_revision = "0019_loyalty_programme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS monthly_order_quota INTEGER NOT NULL DEFAULT 1500
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS order_pack_credits INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS billing_period_started_at TIMESTAMPTZ NOT NULL DEFAULT now()
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS billing_period_started_at")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS order_pack_credits")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS monthly_order_quota")
