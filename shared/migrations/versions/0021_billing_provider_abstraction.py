"""BillingProvider abstraction: PayFast subscription token field

Revision ID: 0021_billing_provider_abstraction
Revises: 0020_monthly_order_quota
Create Date: 2026-07-24

Adds businesses.payfast_subscription_token — the PayFast-provider
equivalent of the existing stripe_subscription_id, needed so
PayFastBillingProvider has somewhere to store what it gets back from a
PayFast recurring-billing checkout.
"""
from alembic import op

revision = "0021_billing_provider"  # shortened to fit alembic_version VARCHAR(32) — see DATABASE.md
down_revision = "0020_monthly_order_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS payfast_subscription_token VARCHAR(255) NULL
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD CONSTRAINT uq_businesses_payfast_subscription_token UNIQUE (payfast_subscription_token)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP CONSTRAINT IF EXISTS uq_businesses_payfast_subscription_token")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS payfast_subscription_token")
