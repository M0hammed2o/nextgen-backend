"""
Inventory: ingredients, suppliers, the append-only stock ledger, and goods
receiving.

Quantity storage rule (enforced everywhere, see backend/app/inventory/units.py):
every ``*_milli`` column is an integer count of thousandths of the
ingredient's canonical unit. 150 g is 150_000. Never a float.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Supplier(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Minimal supplier record - just enough to attribute a goods receipt.
    Purchase orders and a supplier portal are deliberately out of scope.
    """

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("business_id", "name", name="uq_suppliers_business_id_name"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<Supplier {self.name!r}>"


class Ingredient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A physically stocked thing: cheese, dough balls, pizza boxes.

    Distinct from a MenuItem (what a customer buys) - a menu item consumes
    ingredients through a Recipe. An ingredient is never sold directly.
    """

    __tablename__ = "ingredients"
    __table_args__ = (
        UniqueConstraint("business_id", "name", name="uq_ingredients_business_id_name"),
        Index("ix_ingredients_business_active", "business_id", "is_active"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Optional franchise/supplier code"
    )

    canonical_unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="GRAM / MILLILITRE / UNIT - the unit every *_milli column counts",
    )
    count_method: Mapped[str] = mapped_column(
        String(16),
        default="UNIT_COUNT",
        nullable=False,
        comment="UNIT_COUNT / WEIGHT / VOLUME - how staff are told to count it",
    )

    # -- Purchasing ---------------------------------------------------------
    purchase_unit_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Human label for one purchase pack, e.g. '2 kg bag', 'case of 12'",
    )
    purchase_pack_milli: Mapped[int] = mapped_column(
        BigInteger,
        default=1000,
        nullable=False,
        comment="Canonical milli-units in one purchase pack. 2 kg bag = 2_000_000",
    )
    cost_per_pack_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="What one purchase pack costs, in cents"
    )
    cost_per_canonical_micro_cents: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment=(
            "Derived unit cost: micro-cents (cents x 1e6) per canonical unit. "
            "Kept denormalised so ledger valuation never divides at read time."
        ),
    )

    par_level_milli: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Reorder threshold in canonical milli-units; NULL = no alerting",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Ingredient {self.name!r} [{self.canonical_unit}]>"


class StockMovement(Base, UUIDPrimaryKeyMixin):
    """
    One append-only entry in the stock ledger.

    APPEND-ONLY IS A HARD RULE. A movement is never updated and never
    deleted; a correction is always a new compensating movement. This is
    what makes a variance report auditable - and it mirrors the one-way
    close already enforced on TillSession.

    ``quantity_milli`` is signed: negative removes stock (a sale, wastage),
    positive adds it (goods received, a reversal).

    ``occurred_at`` is the *business* time the movement happened, which is
    what a stocktake cut-off compares against - deliberately separate from
    ``created_at``, the row-insertion time.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        # One deduction per checkout, forever - the ledger half of the POS
        # idempotency guarantee. Partial index (WHERE idempotency_key IS NOT
        # NULL) is created in the migration.
        UniqueConstraint(
            "business_id",
            "idempotency_key",
            name="uq_stock_movements_business_id_idempotency_key",
        ),
        Index(
            "ix_stock_movements_business_ingredient_time",
            "business_id",
            "ingredient_id",
            "occurred_at",
        ),
        Index("ix_stock_movements_source", "source_type", "source_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingredients.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="RESTRICT, not CASCADE - deleting an ingredient must never "
        "silently erase the stock history that explains a past variance",
    )

    quantity_milli: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Signed canonical milli-units. Negative = stock leaves.",
    )
    movement_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    source_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Order / GoodsReceipt / WastageEntry / Stocktake id. Not FK-"
        "enforced because it is polymorphic, same pattern as Order.cancelled_by_user_id",
    )

    unit_cost_micro_cents: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Ingredient cost at the moment of the movement, snapshotted so "
        "a later price change never rewrites a historical valuation",
    )
    value_cents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="Signed rand value of this movement"
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="business_users.id"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="Business event time - what a stocktake cut-off compares against",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<StockMovement {self.movement_type} {self.quantity_milli}>"


class GoodsReceipt(Base, UUIDPrimaryKeyMixin):
    """
    A delivery actually received. Quantities recorded here are what was
    *counted on arrival*, never what was ordered - receiving at ordered
    quantity is one of the top causes of unexplained variance weeks later.
    """

    __tablename__ = "goods_receipts"
    __table_args__ = (
        Index("ix_goods_receipts_business_time", "business_id", "occurred_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True
    )
    reference: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Delivery note / invoice number"
    )
    received_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    total_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        "GoodsReceiptLine", back_populates="receipt", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<GoodsReceipt {self.reference!r}>"


class GoodsReceiptLine(Base, UUIDPrimaryKeyMixin):
    """One ingredient line on a delivery."""

    __tablename__ = "goods_receipt_lines"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("goods_receipts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingredients.id", ondelete="RESTRICT"),
        nullable=False,
    )

    entered_quantity: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Exactly what the user typed, e.g. '5' - kept as text for audit",
    )
    entered_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="PACK / KG / G / L / ML / UNIT"
    )
    quantity_milli: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Converted canonical milli-units"
    )

    unit_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    receipt: Mapped["GoodsReceipt"] = relationship("GoodsReceipt", back_populates="lines")

    def __repr__(self) -> str:
        return f"<GoodsReceiptLine {self.quantity_milli}>"
