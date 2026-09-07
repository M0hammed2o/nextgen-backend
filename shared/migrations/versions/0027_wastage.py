"""Wastage entries: reason-coded stock loss capture

Revision ID: 0027_wastage
Revises: 0026_recipes
Create Date: 2026-09-07

Ciao Pizza P0, part 4 of 5.

Unrecorded wastage is mathematically indistinguishable from theft in a
variance report, so every loss carries a reason code. STAFF_MEAL and
COMPLIMENTARY are reasons here but post their own distinct movement types to
the ledger, because they are a cost of doing business rather than a process
failure and need to be reported apart from genuine waste.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027_wastage"
down_revision = "0026_recipes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wastage_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity_milli", sa.BigInteger, nullable=False),
        sa.Column("entered_quantity", sa.String(32), nullable=False),
        sa.Column("entered_unit", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("value_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("photo_asset_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("related_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity_milli > 0", name="ck_wastage_entries_quantity_positive"),
    )
    op.create_index("ix_wastage_entries_business_id", "wastage_entries", ["business_id"])
    op.create_index("ix_wastage_entries_reason", "wastage_entries", ["reason"])
    op.create_index("ix_wastage_entries_business_time", "wastage_entries", ["business_id", "occurred_at"])
    op.create_index(
        "ix_wastage_entries_business_ingredient",
        "wastage_entries",
        ["business_id", "ingredient_id"],
    )


def downgrade() -> None:
    op.drop_table("wastage_entries")
