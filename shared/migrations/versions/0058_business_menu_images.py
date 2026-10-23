"""Multi-page menu images (business_menu_images)

Revision ID: 0058_business_menu_images
Revises: 0057_pos_entitlement
Create Date: 2026-10-23

Purely additive, fully reversible: one new table, no existing column
touched. `businesses.menu_image_url` is left completely alone — every
existing single-image business keeps working unchanged, reading that same
field through the same pipeline.py code path as before. This table is for
businesses that want multiple NAMED, ORDERED pages (e.g. Mochachos
Westville's "Regular Menu — Page 1/2" + "Secret Creations") managed from
the dashboard; it has no WhatsApp-delivery behaviour of its own yet (see
DECISIONS.md for the pending delivery-design decision).

Each row references an existing `assets` row rather than storing a raw
URL — same resolution pattern as `businesses.logo_asset_id`.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0058_business_menu_images"
down_revision = "0057_pos_entitlement"
branch_labels = None
depends_on = None

_TABLE = "business_menu_images"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(f"ix_{_TABLE}_business_id", _TABLE, ["business_id"])
    # Not unique on (business_id, sort_order): reordering writes would need
    # to shuffle through a temporary colliding value otherwise. Ordering
    # ties break on created_at at read time (see routes_menu_images.py).
    op.create_index(f"ix_{_TABLE}_business_id_sort_order", _TABLE, ["business_id", "sort_order"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_business_id_sort_order", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_business_id", table_name=_TABLE)
    op.drop_table(_TABLE)
