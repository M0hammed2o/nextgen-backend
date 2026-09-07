"""
Recipes (bill of materials): how a sellable menu item turns into ingredient
consumption.

A Recipe hangs off EITHER a MenuItem or a MenuAddOn, never both - an add-on
like "Extra Cheese" has its own little recipe (+50 g cheese) that is applied
on top of the base item's recipe.

SIZE HANDLING
-------------
This platform models sizes as an *option group* on a single MenuItem, not as
separate items. So recipe lines carry optional option scoping:

    line with option_id = NULL        -> always applies (base)
    line with option_id = "<opt-id>"  -> applies only when that option is chosen

A pizza whose cheese differs by size therefore has two cheese lines, each
scoped to a size option, and no unscoped cheese line.

REMOVALS
--------
``removable_ref_id`` matches an entry in ``MenuItem.options_json
.removable_ingredients[].id``. When the customer says "no mushroom", the POS
already records that id, and the matching recipe line is skipped - so the
30 g of mushroom is never deducted.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Recipe(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    The active bill of materials for one menu item or add-on.

    Versioned for traceability, but note that historical accuracy does NOT
    depend on versioning: resolved consumption is snapshotted onto the stock
    ledger at sale time, so editing a recipe tomorrow can never rewrite what
    yesterday's orders consumed.
    """

    __tablename__ = "recipes"
    __table_args__ = (
        CheckConstraint(
            "(menu_item_id IS NOT NULL AND menu_add_on_id IS NULL) OR "
            "(menu_item_id IS NULL AND menu_add_on_id IS NOT NULL)",
            name="ck_recipes_exactly_one_parent",
        ),
        Index("ix_recipes_business_item", "business_id", "menu_item_id"),
        Index("ix_recipes_business_add_on", "business_id", "menu_add_on_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    menu_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=True
    )
    menu_add_on_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_add_ons.id", ondelete="CASCADE"), nullable=True
    )

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    yield_qty: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        comment="How many sellable portions one execution of this recipe makes. "
        "1 for a pizza; >1 only for batch-prepped items.",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["RecipeItem"]] = relationship(
        "RecipeItem",
        back_populates="recipe",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Recipe v{self.version} items={len(self.items or [])}>"


class RecipeItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One ingredient line within a recipe."""

    __tablename__ = "recipe_items"
    __table_args__ = (
        Index("ix_recipe_items_recipe", "recipe_id"),
        Index("ix_recipe_items_business_ingredient", "business_id", "ingredient_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipe_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False
    )

    quantity_milli: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Positive canonical milli-units consumed per portion",
    )

    # -- Conditional scoping ------------------------------------------------
    option_group_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Option group this line is scoped to (informational; option_id decides)",
    )
    option_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="When set, this line applies ONLY if that option was selected. "
        "NULL means the line is part of the base recipe.",
    )
    removable_ref_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Matches MenuItem.options_json.removable_ingredients[].id - when "
        "the customer removes that ingredient this line is skipped",
    )

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    recipe: Mapped["Recipe"] = relationship("Recipe", back_populates="items")

    def __repr__(self) -> str:
        return f"<RecipeItem {self.ingredient_id} {self.quantity_milli}>"
