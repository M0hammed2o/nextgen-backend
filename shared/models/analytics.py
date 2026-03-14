"""
Daily usage metrics — one row per business per day.
Updated via upsert increments. Powers analytics dashboards and limit enforcement.
"""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class DailyUsage(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "daily_usage"
    __table_args__ = (
        UniqueConstraint("business_id", "day", name="uq_daily_usage_business_day"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)

    # ── Message Counts ───────────────────────────────────────────────────
    inbound_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outbound_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── LLM Usage ────────────────────────────────────────────────────────
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Order Metrics ────────────────────────────────────────────────────
    orders_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    orders_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revenue_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Customer Metrics ─────────────────────────────────────────────────
    unique_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
