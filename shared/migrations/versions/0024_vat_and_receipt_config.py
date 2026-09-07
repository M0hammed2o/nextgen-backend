"""VAT/tax configuration, tenant receipt template config, inventory master flag

Revision ID: 0024_vat_and_receipt_config
Revises: 0023_pos_checkout_workflow
Create Date: 2026-09-07

Ciao Pizza P0, part 1 of 5.

Until now `orders.tax_cents` was hardcoded to 0 by routes_pos.py because no
tax-rate configuration existed anywhere — meaning a VAT-registered tenant's
till slip was not a valid SA tax invoice. This adds per-business VAT
configuration (rate in basis points, never a float), the tenant receipt
template configuration that drives the thermal slip, and the
`inventory_enabled` master switch.

`inventory_enabled` defaults to FALSE so every existing tenant's POS keeps
behaving exactly as it does today; only a tenant explicitly switched on gets
recipe resolution and stock movements.

Additive only — no existing column is altered or dropped.
"""
from alembic import op

revision = "0024_vat_and_receipt_config"
down_revision = "0023_pos_checkout_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── VAT / tax ────────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS vat_registered BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS vat_number VARCHAR(32) NULL
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS vat_rate_basis_points INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS vat_inclusive_pricing BOOLEAN NOT NULL DEFAULT TRUE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD CONSTRAINT ck_businesses_vat_rate_basis_points_range
            CHECK (vat_rate_basis_points >= 0 AND vat_rate_basis_points <= 10000)
    """)

    # ── Receipt / thermal printing ───────────────────────────────────────
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS receipt_paper_width_mm INTEGER NOT NULL DEFAULT 80
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD CONSTRAINT ck_businesses_receipt_paper_width_mm_supported
            CHECK (receipt_paper_width_mm IN (58, 80))
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS receipt_auto_print BOOLEAN NOT NULL DEFAULT TRUE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS receipt_copies INTEGER NOT NULL DEFAULT 1
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS print_kitchen_receipt BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS receipt_config_json JSONB NULL
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS receipt_reference_asset_id UUID NULL
    """)

    # ── Inventory module master switch ───────────────────────────────────
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS inventory_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS wastage_approval_threshold_cents INTEGER NOT NULL DEFAULT 0
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP CONSTRAINT IF EXISTS ck_businesses_receipt_paper_width_mm_supported")
    op.execute("ALTER TABLE businesses DROP CONSTRAINT IF EXISTS ck_businesses_vat_rate_basis_points_range")
    for column in (
        "wastage_approval_threshold_cents",
        "inventory_enabled",
        "receipt_reference_asset_id",
        "receipt_config_json",
        "print_kitchen_receipt",
        "receipt_copies",
        "receipt_auto_print",
        "receipt_paper_width_mm",
        "vat_inclusive_pricing",
        "vat_rate_basis_points",
        "vat_number",
        "vat_registered",
    ):
        op.execute(f"ALTER TABLE businesses DROP COLUMN IF EXISTS {column}")
