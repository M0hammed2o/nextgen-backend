"""Recipes: menu item / add-on bill of materials with size-option and removal scoping

Revision ID: 0026_recipes
Revises: 0025_inventory_foundation
Create Date: 2026-09-07

Ciao Pizza P0, part 3 of 5.

A recipe belongs to exactly one parent — a menu item OR an add-on — enforced
by ck_recipes_exactly_one_parent.

Recipe lines carry optional option scoping because this platform models
sizes as an option group on a single menu item, not as separate items. A
line scoped to the "Large" option only applies when Large is selected, which
is what makes a large pizza deduct 150 g of cheese where a small deducts
100 g. `removable_ref_id` matches an id in
menu_items.options_json.removable_ingredients so "no mushroom" skips that
line entirely.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_recipes"
down_revision = "0025_inventory_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("menu_add_on_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("menu_add_ons.id", ondelete="CASCADE"), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("yield_qty", sa.Integer, nullable=False, server_default="1"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(menu_item_id IS NOT NULL AND menu_add_on_id IS NULL) OR "
            "(menu_item_id IS NULL AND menu_add_on_id IS NOT NULL)",
            name="ck_recipes_exactly_one_parent",
        ),
        sa.CheckConstraint("yield_qty > 0", name="ck_recipes_yield_positive"),
    )
    op.create_index("ix_recipes_business_id", "recipes", ["business_id"])
    op.create_index("ix_recipes_business_item", "recipes", ["business_id", "menu_item_id"])
    op.create_index("ix_recipes_business_add_on", "recipes", ["business_id", "menu_add_on_id"])
    # Only one ACTIVE recipe per parent — resolution must never be ambiguous.
    op.execute("""
        CREATE UNIQUE INDEX uq_recipes_active_menu_item
            ON recipes (menu_item_id)
            WHERE is_active AND menu_item_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_recipes_active_menu_add_on
            ON recipes (menu_add_on_id)
            WHERE is_active AND menu_add_on_id IS NOT NULL
    """)

    op.create_table(
        "recipe_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipe_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity_milli", sa.BigInteger, nullable=False),
        sa.Column("option_group_id", sa.String(64), nullable=True),
        sa.Column("option_id", sa.String(64), nullable=True),
        sa.Column("removable_ref_id", sa.String(64), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity_milli > 0", name="ck_recipe_items_quantity_positive"),
    )
    op.create_index("ix_recipe_items_business_id", "recipe_items", ["business_id"])
    op.create_index("ix_recipe_items_recipe", "recipe_items", ["recipe_id"])
    op.create_index("ix_recipe_items_business_ingredient", "recipe_items", ["business_id", "ingredient_id"])


def downgrade() -> None:
    op.drop_table("recipe_items")
    op.execute("DROP INDEX IF EXISTS uq_recipes_active_menu_add_on")
    op.execute("DROP INDEX IF EXISTS uq_recipes_active_menu_item")
    op.drop_table("recipes")
