"""
Menu system — categories and items.
Categories are a dedicated table (not just a text field) for proper management.
Menu items use soft delete to preserve order history references.
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
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

    options_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment='e.g. {"sizes": [{"name": "Small", "price_cents": 2500}, ...]}'
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

    def __repr__(self) -> str:
        return f"<MenuItem {self.name} ({self.price_cents}c)>"
