"""
Orders — the core transactional entity.
Includes order items (snapshots at time of order) and audit events.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Order(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "orders"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # ── Order Number (human-friendly, unique per business) ───────────────
    order_number: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="Human-friendly: BO-000123"
    )

    # ── Status ───────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(32), default="NEW", nullable=False, index=True
    )
    payment_status: Mapped[str] = mapped_column(
        String(32), default="PENDING", nullable=False,
        comment="PENDING / PAID / CASH_ON_COLLECTION"
    )

    # ── Order Details ────────────────────────────────────────────────────
    order_mode: Mapped[str] = mapped_column(
        String(32), default="PICKUP", nullable=False,
        comment="PICKUP / DELIVERY / DINE_IN"
    )
    source: Mapped[str] = mapped_column(
        String(32), default="WHATSAPP", nullable=False,
        comment="WHATSAPP / MANUAL / ADMIN"
    )

    # ── Money (cents) ────────────────────────────────────────────────────
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="ZAR", nullable=False)

    # ── Customer Info (snapshot) ─────────────────────────────────────────
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    estimated_ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Cancellation ─────────────────────────────────────────────────────
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Relationships ────────────────────────────────────────────────────
    items = relationship("OrderItem", back_populates="order", lazy="selectin")
    events = relationship("OrderEvent", back_populates="order", lazy="noload")

    def __repr__(self) -> str:
        return f"<Order {self.order_number} [{self.status}]>"


class OrderItem(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    menu_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Nullable — menu item may have been deleted (soft delete preserves this)"
    )

    # ── Snapshot at time of order (immutable) ────────────────────────────
    name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    options_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    special_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Relationships ────────────────────────────────────────────────────
    order = relationship("Order", back_populates="items")


class OrderEvent(Base, UUIDPrimaryKeyMixin):
    """Audit trail for order status changes."""
    __tablename__ = "order_events"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    # ── Relationships ────────────────────────────────────────────────────
    order = relationship("Order", back_populates="events")
