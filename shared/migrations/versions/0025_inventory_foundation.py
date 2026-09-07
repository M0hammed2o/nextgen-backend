"""Inventory foundation: ingredients, suppliers, append-only stock ledger, goods receiving

Revision ID: 0025_inventory_foundation
Revises: 0024_vat_and_receipt_config
Create Date: 2026-09-07

Ciao Pizza P0, part 2 of 5.

Creates the stock domain that has never existed in this platform. Until now
the only trace of inventory anywhere was `menu_add_ons.inventory_item_id`, a
nullable placeholder column added in migration 0006 that pointed at a table
which was never created (see its own comment). That dangling column is
resolved here: it is repointed at the real `ingredients` table now that one
exists, rather than being left as a permanently dead reference.

QUANTITY STORAGE
----------------
Every *_milli column is BIGINT holding thousandths of the ingredient's
canonical unit (GRAM / MILLILITRE / UNIT). 150 g is 150000. Integers, for
the same reason money is stored in cents: float drift in a stock ledger is
indistinguishable from theft in a variance report.

THE LEDGER IS APPEND-ONLY
-------------------------
`stock_movements` rows are never updated or deleted by the application. A
correction is a new compensating movement. The partial unique index on
(business_id, idempotency_key) is the ledger half of the POS checkout
idempotency guarantee: a double-tapped till button can never deduct twice.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025_inventory_foundation"
down_revision = "0024_vat_and_receipt_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Suppliers ────────────────────────────────────────────────────────
    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("business_id", "name", name="uq_suppliers_business_id_name"),
    )
    op.create_index("ix_suppliers_business_id", "suppliers", ["business_id"])

    # ── Ingredients ──────────────────────────────────────────────────────
    op.create_table(
        "ingredients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sku", sa.String(64), nullable=True),
        sa.Column("canonical_unit", sa.String(16), nullable=False),
        sa.Column("count_method", sa.String(16), nullable=False, server_default="UNIT_COUNT"),
        sa.Column("purchase_unit_name", sa.String(64), nullable=True),
        sa.Column("purchase_pack_milli", sa.BigInteger, nullable=False, server_default="1000"),
        sa.Column("cost_per_pack_cents", sa.Integer, nullable=True),
        sa.Column("cost_per_canonical_micro_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("par_level_milli", sa.BigInteger, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("business_id", "name", name="uq_ingredients_business_id_name"),
        sa.CheckConstraint(
            "canonical_unit IN ('GRAM', 'MILLILITRE', 'UNIT')",
            name="ck_ingredients_canonical_unit_known",
        ),
        sa.CheckConstraint(
            "count_method IN ('UNIT_COUNT', 'WEIGHT', 'VOLUME')",
            name="ck_ingredients_count_method_known",
        ),
        sa.CheckConstraint("purchase_pack_milli > 0", name="ck_ingredients_purchase_pack_positive"),
    )
    op.create_index("ix_ingredients_business_id", "ingredients", ["business_id"])
    op.create_index("ix_ingredients_business_active", "ingredients", ["business_id", "is_active"])

    # ── Append-only stock ledger ─────────────────────────────────────────
    op.create_table(
        "stock_movements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity_milli", sa.BigInteger, nullable=False),
        sa.Column("movement_type", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("unit_cost_micro_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("value_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_stock_movements_business_id", "stock_movements", ["business_id"])
    op.create_index("ix_stock_movements_ingredient_id", "stock_movements", ["ingredient_id"])
    op.create_index("ix_stock_movements_movement_type", "stock_movements", ["movement_type"])
    op.create_index(
        "ix_stock_movements_business_ingredient_time",
        "stock_movements",
        ["business_id", "ingredient_id", "occurred_at"],
    )
    op.create_index("ix_stock_movements_source", "stock_movements", ["source_type", "source_id"])
    # Partial: only rows that actually carry a key participate, so the many
    # movements with no idempotency key (wastage, receipts) don't collide.
    op.execute("""
        CREATE UNIQUE INDEX uq_stock_movements_business_id_idempotency_key
            ON stock_movements (business_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)

    # ── Goods receiving ──────────────────────────────────────────────────
    op.create_table(
        "goods_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("received_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_cost_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_goods_receipts_business_id", "goods_receipts", ["business_id"])
    op.create_index("ix_goods_receipts_business_time", "goods_receipts", ["business_id", "occurred_at"])

    op.create_table(
        "goods_receipt_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goods_receipt_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("entered_quantity", sa.String(32), nullable=False),
        sa.Column("entered_unit", sa.String(32), nullable=False),
        sa.Column("quantity_milli", sa.BigInteger, nullable=False),
        sa.Column("unit_cost_cents", sa.Integer, nullable=True),
        sa.Column("line_cost_cents", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_goods_receipt_lines_business_id", "goods_receipt_lines", ["business_id"])
    op.create_index("ix_goods_receipt_lines_goods_receipt_id", "goods_receipt_lines", ["goods_receipt_id"])

    # ── Resolve the dangling 0006 placeholder ────────────────────────────
    # menu_add_ons.inventory_item_id has been a nullable UUID pointing at a
    # non-existent table since 0006. Now that ingredients exists, give it a
    # real FK instead of leaving a permanently dead reference in the schema.
    op.execute("""
        ALTER TABLE menu_add_ons
            ADD CONSTRAINT fk_menu_add_ons_inventory_item_id_ingredients
            FOREIGN KEY (inventory_item_id) REFERENCES ingredients (id)
            ON DELETE SET NULL
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE menu_add_ons DROP CONSTRAINT IF EXISTS fk_menu_add_ons_inventory_item_id_ingredients")
    op.drop_table("goods_receipt_lines")
    op.drop_table("goods_receipts")
    op.execute("DROP INDEX IF EXISTS uq_stock_movements_business_id_idempotency_key")
    op.drop_table("stock_movements")
    op.drop_table("ingredients")
    op.drop_table("suppliers")
