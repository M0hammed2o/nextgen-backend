"""
Till sessions — cash-up (open/close, variance reconciliation) for POS.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class TillSession(Base, UUIDPrimaryKeyMixin):
    """
    One cash-up period for a business: opened with a starting float,
    closed with a counted cash amount and manager sign-off.

    Closing is a one-way transition — the app layer never updates a
    CLOSED session in place, so historical cash-up records can't be
    silently altered after the fact.
    """
    __tablename__ = "till_sessions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    opened_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="business_users.id — not FK-enforced, same pattern as Order.cancelled_by_user_id"
    )
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Manager/owner who signed off the close (require_owner_or_manager enforces this)"
    )

    opening_float_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_cash_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counted_cash_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variance_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default="OPEN", nullable=False, index=True,
        comment="OPEN / CLOSED"
    )

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<TillSession {self.id} [{self.status}]>"
