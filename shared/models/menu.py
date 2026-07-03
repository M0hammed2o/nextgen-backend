"""
Menu system — categories, items, and paid add-ons.
Categories are a dedicated table (not just a text field) for proper management.
Menu items use soft delete to preserve order history references.

Phase 8: MenuAddOn and MenuItemAddOn added for the unified pricing engine.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, Table, Column
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MenuCategory(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "menu_categories"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Relationships ────────────────────────────────────────────────────
    business = relationship("Business", back_populates="menu_categories")
    items = relationship("MenuItem", back_populates="category_rel", lazy="selectin")

    def __repr__(self) -> str:
        return f"<MenuCategory {self.name}>"


# ── Many-to-many join: which add-ons are available on which menu items ────────
menu_item_add_ons = Table(
    "menu_item_add_ons",
    Base.metadata,
    Column(
        "menu_item_id",
        UUID(as_uuid=True),
        ForeignKey("menu_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "add_on_id",
        UUID(as_uuid=True),
        ForeignKey("menu_add_ons.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class MenuAddOn(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A priced add-on that can be offered on one or more menu items.

    Examples: Extra Cheese (+R10), Extra Patty (+R25), Extra Shot (+R15).

    inventory_item_id is a nullable placeholder for a future inventory system —
    a future migration can add the FK without touching this model.
    """
    __tablename__ = "menu_add_ons"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Quantity controls ────────────────────────────────────────────────
    min_qty: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Minimum units per order line (0 = optional)"
    )
    max_qty: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False,
        comment="Maximum units per order line"
    )
    default_qty: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False,
        comment="Default quantity when customer says 'add extra cheese'"
    )

    # ── Future: inventory linking (nullable until inventory module exists) ─
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Future FK to inventory_items.id for automatic stock deduction"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Soft delete — preserves order history"
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Relationships ────────────────────────────────────────────────────
    menu_items = relationship(
        "MenuItem",
        secondary=menu_item_add_ons,
        back_populates="add_ons",
        lazy="noload",
    )

    def __repr__(self) -> str:
        return f"<MenuAddOn {self.name} ({self.price_cents}c)>"


class MenuItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "menu_items"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_categories.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ZAR", nullable=False)

    # ── Options (sizes, milk types, etc.) ────────────────────────────────
    # Schema: {"option_groups": [{
    #   "id": str, "name": str, "required": bool,
    #   "min_selections": int, "max_selections": int,
    #   "sort_order": int, "is_enabled": bool, "default_option_id": str|null,
    #   "options": [{"id": str, "name": str, "price_delta_cents": int,
    #                "sort_order": int, "is_enabled": bool}]
    # }]}
    # price_delta_cents: signed integer — 0 = free modifier, +N = more expensive, -N = cheaper
    options_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment='Option groups with per-option price_delta_cents'
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Soft delete — preserves order history references"
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────
    business = relationship("Business", back_populates="menu_items")
    category_rel = relationship("MenuCategory", back_populates="items")
    add_ons = relationship(
        "MenuAddOn",
        secondary=menu_item_add_ons,
        back_populates="menu_items",
        lazy="selectin",
        order_by="MenuAddOn.sort_order",
    )

    def __repr__(self) -> str:
        return f"<MenuItem {self.name} ({self.price_cents}c)>"
