"""Add advanced_analytics_enabled to businesses

Revision ID: 0047_advanced_analytics
Revises: 0046_delivery_auto_accept
Create Date: 2026-09-23

Commercial V1 release gate: NextGen POS (R899) gets Core Analytics; NextGen
Complete (from R2,499) gets Advanced Analytics. Before this migration there
was no code-level entitlement distinction at all — a POS-only tenant and a
Complete tenant hit identical analytics endpoints, so "Advanced Analytics"
was a marketing label with nothing enforcing it (see
docs/COMMERCIAL_FEATURE_MATRIX_2026-10.md's Analytics section, "Note on
Analytics tiering").

`advanced_analytics_enabled` follows the exact same pattern as
`inventory_enabled`/`digital_receipts_enabled`/`delivery_platforms_enabled`:
platform-admin-writable (admin_api), business-plane read-only. Purely
additive, one NOT NULL boolean column with a server_default, fully
reversible — same profile as every other entitlement migration in this
chain (see 0039/0044).
"""
from alembic import op
import sqlalchemy as sa

revision = "0047_advanced_analytics"
down_revision = "0046_delivery_auto_accept"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "advanced_analytics_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS advanced_analytics_enabled")
