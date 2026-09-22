"""Add delivery entitlement flags to businesses

Revision ID: 0044_delivery_entitlements
Revises: 0043_delivery_mapping_extension
Create Date: 2026-09-22

Fifth and last of the delivery-marketplace migrations. Purely additive,
three NOT NULL boolean columns with server_default false, fully reversible.

delivery_platforms_enabled / uber_eats_enabled / mrd_enabled — see
shared/models/business.py for the full rationale. NOT the same thing as the
pre-existing `delivery_enabled` column (WhatsApp bot own-driver delivery) —
deliberately different names, per docs/DELIVERY_PLATFORM_ARCHITECTURE.md §0.3,
so this migration does not touch that column at all.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044_delivery_entitlements"
down_revision = "0043_delivery_mapping_extension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "delivery_platforms_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "uber_eats_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "mrd_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS mrd_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS uber_eats_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS delivery_platforms_enabled")
