"""
Customers (WhatsApp contacts) and conversation sessions.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Customer(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (
        # wa_id is unique per business (same person can be customer of multiple businesses)
        {"comment": "WhatsApp customers, scoped per business"},
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    wa_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="WhatsApp user identifier (phone number format). Nullable since "
                 "2026-07-24 (migration 0019) — a POS-originated customer enrolled "
                 "for loyalty may have no WhatsApp identity, only a phone_number."
    )
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Loyalty (added 2026-07-24, see backend/app/loyalty/*) ────────────
    loyalty_points_balance: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Current redeemable point balance — 0 for every customer unless the business enables loyalty"
    )
    loyalty_tier: Mapped[str] = mapped_column(
        String(16), default="BRONZE", nullable=False,
        comment="BRONZE / SILVER / GOLD, computed from loyalty_points_balance against LoyaltyProgram thresholds"
    )
    loyalty_points_expire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the current balance expires (simple rolling expiry, not per-transaction FIFO — see DECISIONS.md)"
    )


# Composite unique index: one wa_id per business
from sqlalchemy import UniqueConstraint
Customer.__table_args__ = (
    UniqueConstraint("business_id", "wa_id", name="uq_customers_business_wa_id"),
    {"comment": "WhatsApp customers, scoped per business"},
)


class ConversationSession(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "conversation_sessions"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    state: Mapped[str] = mapped_column(
        String(64), default="IDLE", nullable=False,
        comment="Current conversation state machine state"
    )
    context_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Cart contents, pending questions, collected details"
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Session auto-expires after inactivity"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
