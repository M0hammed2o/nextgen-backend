"""
Wastage - recorded loss of stock outside a sale.

Unrecorded wastage is mathematically indistinguishable from theft, so every
loss needs a reason code. An OTHER entry without a note is exactly the kind
of unexplainable variance this whole subsystem exists to eliminate, which is
why the API requires a note for OTHER.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class WastageEntry(Base, UUIDPrimaryKeyMixin):
    """
    One recorded loss. Posts a negative StockMovement of type WASTAGE
    (or STAFF_MEAL / COMPLIMENTARY, which are tracked separately because
    they are a cost of doing business rather than a process failure).
    """

    __tablename__ = "wastage_entries"
    __table_args__ = (
        Index("ix_wastage_entries_business_time", "business_id", "occurred_at"),
        Index("ix_wastage_entries_business_ingredient", "business_id", "ingredient_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False
    )

    quantity_milli: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Positive magnitude wasted; the ledger movement is negative",
    )
    entered_quantity: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="What the user actually typed"
    )
    entered_unit: Mapped[str] = mapped_column(String(32), nullable=False)

    reason: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="business_users.id"
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Manager sign-off, required above Business.wastage_approval_threshold_cents",
    )
    photo_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    related_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="Set for REMAKE wastage so a remake can be traced to its sale",
    )

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

    def __repr__(self) -> str:
        return f"<WastageEntry {self.reason} {self.quantity_milli}>"
