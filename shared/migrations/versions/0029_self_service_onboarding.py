"""Self-service onboarding: ingredient unit labels/categories, reference asset kinds

Revision ID: 0029_self_service_onboarding
Revises: 0028_stocktake
Create Date: 2026-09-07

Supports restaurants configuring themselves without developer help.

`ingredients.unit_label` is the noun staff actually say for a countable
ingredient - "portion", "box", "sachet", "ball". It is display-only; the
ledger still counts canonical units, so deterministic arithmetic is
unchanged. A prepacked franchise counts portions rather than grams, and a
stock screen that shows a bare "72" instead of "72 portions" is one nobody
keeps up to date.

`ingredients.category` groups a long ingredient list ("Dairy", "Packaging").

`businesses.menu_reference_asset_id` stores an uploaded photo/PDF of the
restaurant's existing menu as an onboarding reference, mirroring
`receipt_reference_asset_id` added in 0024. Neither is ever rendered to a
customer - both exist so we can reproduce a layout the client already has.

Additive only.
"""
from alembic import op

revision = "0029_self_service_onboarding"
down_revision = "0028_stocktake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ingredients
            ADD COLUMN IF NOT EXISTS unit_label VARCHAR(32) NULL
    """)
    op.execute("""
        ALTER TABLE ingredients
            ADD COLUMN IF NOT EXISTS category VARCHAR(64) NULL
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS menu_reference_asset_id UUID NULL
    """)

    # Backfill a sensible noun for existing countable ingredients so the new
    # display path reads naturally from the first page load rather than only
    # for rows created after this migration.
    op.execute("""
        UPDATE ingredients
           SET unit_label = 'unit'
         WHERE canonical_unit = 'UNIT'
           AND unit_label IS NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS menu_reference_asset_id")
    op.execute("ALTER TABLE ingredients DROP COLUMN IF EXISTS category")
    op.execute("ALTER TABLE ingredients DROP COLUMN IF EXISTS unit_label")
