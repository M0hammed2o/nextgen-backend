"""Payment provider credentials per business (Model B)

Revision ID: 0009_payment_provider_credentials
Revises: 0008_payment_settings
Create Date: 2026-07-07

Adds three columns to businesses for storing per-business payment provider
credentials. Credentials are stored as plain text; encrypt at rest via
Postgres column-level encryption or a secrets manager before going to prod
at significant scale.

Field mapping per provider:
  Yoco:    payment_api_key=Secret Key,  payment_api_secret=<unused>,    payment_webhook_secret=Webhook Secret
  PayFast: payment_api_key=Merchant Key, payment_api_secret=Merchant ID, payment_webhook_secret=Passphrase
  Stitch:  payment_api_key=Client Secret, payment_api_secret=Client ID,  payment_webhook_secret=<unused>

All columns use IF NOT EXISTS — safe to re-run.
"""
from alembic import op


revision = "0009_payment_provider_credentials"
down_revision = "0008_payment_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS payment_api_key      VARCHAR(512),
            ADD COLUMN IF NOT EXISTS payment_api_secret   VARCHAR(512),
            ADD COLUMN IF NOT EXISTS payment_webhook_secret VARCHAR(512);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            DROP COLUMN IF EXISTS payment_api_key,
            DROP COLUMN IF EXISTS payment_api_secret,
            DROP COLUMN IF EXISTS payment_webhook_secret;
    """)
