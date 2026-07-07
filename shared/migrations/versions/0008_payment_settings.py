"""Payment settings and online payment flow

Revision ID: 0008_payment_settings
Revises: 0007_add_payment_fields
Create Date: 2026-07-07

Changes:
  businesses table:
    - payment_methods_enabled  JSONB  NULL  (list of enabled payment methods)
    - online_payment_required  BOOLEAN NOT NULL DEFAULT FALSE
    - payment_provider         VARCHAR(32) NULL  (YOCO | PAYFAST | STITCH)
    - payment_timeout_minutes  INTEGER NOT NULL DEFAULT 30
    - eft_bank_name            VARCHAR(255) NULL
    - eft_account_name         VARCHAR(255) NULL
    - eft_account_number       VARCHAR(64)  NULL
    - eft_branch_code          VARCHAR(16)  NULL
    - eft_reference_prefix     VARCHAR(16)  NULL

  orders table:
    - payment_required    BOOLEAN NOT NULL DEFAULT FALSE
    - payment_link_url    VARCHAR(1024) NULL
    - payment_method widened from VARCHAR(16) to VARCHAR(32)

All columns use IF NOT EXISTS / safe defaults so re-running is idempotent.
"""
from alembic import op


revision = "0008_payment_settings"
down_revision = "0007_add_payment_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── businesses: payment config columns ──────────────────────────────────
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS payment_methods_enabled  JSONB,
            ADD COLUMN IF NOT EXISTS online_payment_required  BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS payment_provider         VARCHAR(32),
            ADD COLUMN IF NOT EXISTS payment_timeout_minutes  INTEGER NOT NULL DEFAULT 30,
            ADD COLUMN IF NOT EXISTS eft_bank_name            VARCHAR(255),
            ADD COLUMN IF NOT EXISTS eft_account_name         VARCHAR(255),
            ADD COLUMN IF NOT EXISTS eft_account_number       VARCHAR(64),
            ADD COLUMN IF NOT EXISTS eft_branch_code          VARCHAR(16),
            ADD COLUMN IF NOT EXISTS eft_reference_prefix     VARCHAR(16);
    """)

    # ── orders: new columns + widen payment_method ──────────────────────────
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_required  BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS payment_link_url  VARCHAR(1024);
    """)

    # Widen payment_method from VARCHAR(16) to VARCHAR(32) so PAY_ON_COLLECTION fits
    op.execute("""
        ALTER TABLE orders
            ALTER COLUMN payment_method TYPE VARCHAR(32);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            DROP COLUMN IF EXISTS payment_methods_enabled,
            DROP COLUMN IF EXISTS online_payment_required,
            DROP COLUMN IF EXISTS payment_provider,
            DROP COLUMN IF EXISTS payment_timeout_minutes,
            DROP COLUMN IF EXISTS eft_bank_name,
            DROP COLUMN IF EXISTS eft_account_name,
            DROP COLUMN IF EXISTS eft_account_number,
            DROP COLUMN IF EXISTS eft_branch_code,
            DROP COLUMN IF EXISTS eft_reference_prefix;
    """)

    op.execute("""
        ALTER TABLE orders
            DROP COLUMN IF EXISTS payment_required,
            DROP COLUMN IF EXISTS payment_link_url;
    """)

    op.execute("""
        ALTER TABLE orders
            ALTER COLUMN payment_method TYPE VARCHAR(16);
    """)
