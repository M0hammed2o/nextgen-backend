"""
Product variants (clothing / retail): size x colour style sellable options of
a MenuItem, each with its own SKU, optional barcode, optional price override
and its own stock.

Stock is NOT a new table. A variant points at one ``Ingredient`` (canonical
unit UNIT, 1 each = 1000 milli) so stocktake, wastage, goods receipt, par
levels and the append-only ledger all work per variant unchanged. A sale of
variant units posts consumption of that ingredient; see
backend/app/inventory/consumption.py (``variant_ingredient_id``).
"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProductVariant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("business_id", "sku", name="uq_product_variants_business_id_sku"),
        Index(
            "uq_product_variants_business_id_barcode",
            "business_id", "barcode",
            unique=True,
            postgresql_where=text("barcode IS NOT NULL"),
        ),
        Index("ix_product_variants_menu_item_id", "menu_item_id"),
        CheckConstraint("price_cents IS NULL OR price_cents >= 0", name="price_non_negative"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False,
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
        comment='e.g. {"size": "M", "colour": "Black"}',
    )
    price_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Override; NULL = the menu item's base price"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingredient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="SET NULL"), nullable=True,
        comment="The stock-keeping ingredient (unit UNIT). NULL = stock not tracked.",
    )

    @property
    def label(self) -> str:
        return variant_label(self.attributes)


def variant_label(attributes: dict | None) -> str:
    """'M / Black' — values in key order, stable for receipts and snapshots."""
    return " / ".join(str(v) for v in (attributes or {}).values())
