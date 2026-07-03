"""Phase 8: Pricing engine — add-on tables + order_items breakdown columns

Revision ID: 0006_phase8_pricing_engine
Revises: 0005_unique_conversation_session
Create Date: 2026-07-02

Changes:
  - Creates menu_add_ons table (priced add-ons with quantity limits)
  - Creates menu_item_add_ons join table (which add-ons are available on which items)
  - Adds base_price_cents, option_adjustment_cents, add_on_total_cents,
    selected_options_snapshot, add_ons_snapshot to order_items
  - Backfills base_price_cents = unit_price_cents for existing rows (safe default)

All new order_items columns are nullable or have DEFAULT 0, so existing rows
are fully backward compatible.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0006_phase8_pricing_engine"
down_revision = "0005_unique_conversation_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── menu_add_ons ─────────────────────────────────────────────────────────
    op.create_table(
        "menu_add_ons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id",
            UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("min_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_qty", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("default_qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("inventory_item_id", UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_menu_add_ons_business_id", "menu_add_ons", ["business_id"])

    # ── menu_item_add_ons (join table) ────────────────────────────────────────
    op.create_table(
        "menu_item_add_ons",
        sa.Column(
            "menu_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("menu_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "add_on_id",
            UUID(as_uuid=True),
            sa.ForeignKey("menu_add_ons.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # ── order_items: pricing breakdown columns ───────────────────────────────
    op.add_column(
        "order_items",
        sa.Column(
            "base_price_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="MenuItem.price_cents at order time, before any adjustments",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "option_adjustment_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Sum of selected option price_delta_cents (may be negative)",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "add_on_total_cents",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Sum of add-on price_cents × quantity for this line item",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "selected_options_snapshot",
            JSONB,
            nullable=True,
            comment="[{group_id, group_name, option_id, option_name, price_delta_cents}]",
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "add_ons_snapshot",
            JSONB,
            nullable=True,
            comment="[{add_on_id, name, price_cents, quantity}]",
        ),
    )

    # Backfill: existing rows get base_price_cents = unit_price_cents
    op.execute("UPDATE order_items SET base_price_cents = unit_price_cents")


def downgrade() -> None:
    op.drop_column("order_items", "add_ons_snapshot")
    op.drop_column("order_items", "selected_options_snapshot")
    op.drop_column("order_items", "add_on_total_cents")
    op.drop_column("order_items", "option_adjustment_cents")
    op.drop_column("order_items", "base_price_cents")
    op.drop_table("menu_item_add_ons")
    op.drop_index("ix_menu_add_ons_business_id", table_name="menu_add_ons")
    op.drop_table("menu_add_ons")
