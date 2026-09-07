"""
Stocktake - the physical count that closes a stock period, and the variance
it produces against what the ledger expected.

Modelled deliberately on TillSession (shared/models/till.py): open with a
defined starting position, capture a counted figure, compute variance,
require manager sign-off, and make approval one-way so history cannot be
quietly rewritten. Cash variance and stock variance then behave identically
for a manager, which is worth real money in floor training time.

CUT-OFF
-------
``cutoff_at`` is the moment the count is deemed to represent. Only ledger
movements with ``occurred_at <= cutoff_at`` count toward the expected
quantity; a sale rung up while staff are still counting belongs to the next
period. Without this, counting during service silently corrupts the count.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
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

from shared.models.base import Base, UUIDPrimaryKeyMixin


class StocktakeSession(Base, UUIDPrimaryKeyMixin):
    """
    One counting period.

    DRAFT -> COMPLETED -> APPROVED is one-way. An APPROVED session is
    immutable: the app layer refuses to modify it or its lines, exactly as
    TillSession refuses to reopen a CLOSED cash-up.
    """

    __tablename__ = "stocktake_sessions"
    __table_args__ = (
        Index("ix_stocktake_sessions_business_status", "business_id", "status"),
        Index("ix_stocktake_sessions_business_cutoff", "business_id", "cutoff_at"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    reference: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Human label, e.g. 'Week 36 count'"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="DRAFT",
        nullable=False,
        index=True,
        comment="DRAFT / COMPLETED / APPROVED / CANCELLED",
    )

    cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Movements at or before this instant belong to this count",
    )
    period_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Cut-off of the previous approved count; NULL for the first ever count",
    )

    opened_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    completed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Rolled-up variance, computed once at completion ---------------------
    total_variance_value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_theoretical_value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    lines: Mapped[list["StocktakeLine"]] = relationship(
        "StocktakeLine",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def is_locked(self) -> bool:
        """APPROVED counts are immutable, forever."""
        return self.status == "APPROVED"

    def __repr__(self) -> str:
        return f"<StocktakeSession {self.reference!r} [{self.status}]>"


class StocktakeLine(Base, UUIDPrimaryKeyMixin):
    """
    One ingredient on a count sheet.

    Expected/theoretical figures are snapshotted at completion so the
    variance a manager approved can always be reproduced exactly, even if
    the ledger gains later corrections.
    """

    __tablename__ = "stocktake_lines"
    __table_args__ = (
        UniqueConstraint(
            "stocktake_session_id",
            "ingredient_id",
            name="uq_stocktake_lines_stocktake_session_id_ingredient_id",
        ),
        Index("ix_stocktake_lines_business", "business_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stocktake_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("stocktake_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ingredient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False
    )

    # -- What staff counted --------------------------------------------------
    counted_quantity_milli: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="NULL while the line is still uncounted"
    )
    entered_quantity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entered_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    count_method_snapshot: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="How staff were told to count it"
    )

    # -- What the ledger expected (snapshotted at completion) ----------------
    opening_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    received_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    theoretical_consumption_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    wastage_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    adjustment_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    expected_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # -- Variance ------------------------------------------------------------
    variance_quantity_milli: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    variance_value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    variance_basis_points: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Variance as basis points of theoretical consumption "
        "(200 = 2.00%). NULL when there was no consumption to divide by.",
    )

    unit_cost_micro_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped["StocktakeSession"] = relationship("StocktakeSession", back_populates="lines")

    def __repr__(self) -> str:
        return f"<StocktakeLine {self.ingredient_id} var={self.variance_quantity_milli}>"
