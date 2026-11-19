"""Product variants (clothing/retail): product_variants, order_items variant snapshot, exchange link

Revision ID: 0066_product_variants
Revises: 0065_order_notify_when_ready
Create Date: 2026-11-19

Adds:
- product_variants: sellable size/colour options of a menu item, with SKU and
  barcode (both unique per business), optional price override and a link to the
  ingredient that holds its stock (reuses the existing inventory ledger).
- order_items.variant_id / variant_label_snapshot / sku_snapshot: what was sold.
- order_items.returned_quantity: units returned via a line-level POS refund.
- orders.exchange_of_order_id: an exchange sale points at the refunded order.

Every constraint name is passed through op.f() (metadata naming convention).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066_product_variants"
down_revision = "0065_order_notify_when_ready"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("barcode", sa.String(64), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_variants")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE",
                                name=op.f("fk_product_variants_business_id_businesses")),
        sa.ForeignKeyConstraint(["menu_item_id"], ["menu_items.id"], ondelete="CASCADE",
                                name=op.f("fk_product_variants_menu_item_id_menu_items")),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], ondelete="SET NULL",
                                name=op.f("fk_product_variants_ingredient_id_ingredients")),
        sa.UniqueConstraint("business_id", "sku", name=op.f("uq_product_variants_business_id_sku")),
        sa.CheckConstraint("price_cents IS NULL OR price_cents >= 0",
                           name=op.f("ck_product_variants_price_non_negative")),
    )
    op.create_index(op.f("ix_product_variants_business_id"), "product_variants", ["business_id"])
    op.create_index(op.f("ix_product_variants_menu_item_id"), "product_variants", ["menu_item_id"])
    op.create_index(
        op.f("uq_product_variants_business_id_barcode"), "product_variants", ["business_id", "barcode"],
        unique=True, postgresql_where=sa.text("barcode IS NOT NULL"),
    )

    op.add_column("order_items", sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("order_items", sa.Column("variant_label_snapshot", sa.String(255), nullable=True))
    op.add_column("order_items", sa.Column("sku_snapshot", sa.String(64), nullable=True))
    op.add_column("order_items", sa.Column("returned_quantity", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key(
        op.f("fk_order_items_variant_id_product_variants"), "order_items", "product_variants",
        ["variant_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(op.f("ix_order_items_variant_id"), "order_items", ["variant_id"])

    op.add_column("orders", sa.Column("exchange_of_order_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_orders_exchange_of_order_id_orders"), "orders", "orders",
        ["exchange_of_order_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(op.f("ix_orders_exchange_of_order_id"), "orders", ["exchange_of_order_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_orders_exchange_of_order_id"), table_name="orders")
    op.drop_constraint(op.f("fk_orders_exchange_of_order_id_orders"), "orders", type_="foreignkey")
    op.drop_column("orders", "exchange_of_order_id")

    op.drop_index(op.f("ix_order_items_variant_id"), table_name="order_items")
    op.drop_constraint(op.f("fk_order_items_variant_id_product_variants"), "order_items", type_="foreignkey")
    for col in ("returned_quantity", "sku_snapshot", "variant_label_snapshot", "variant_id"):
        op.drop_column("order_items", col)

    op.drop_index(op.f("uq_product_variants_business_id_barcode"), table_name="product_variants")
    op.drop_index(op.f("ix_product_variants_menu_item_id"), table_name="product_variants")
    op.drop_index(op.f("ix_product_variants_business_id"), table_name="product_variants")
    op.drop_table("product_variants")
