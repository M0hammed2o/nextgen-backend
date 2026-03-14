"""
Specials — schedule-based promotions with day-of-week rules.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Special(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "specials"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Scheduling ───────────────────────────────────────────────────────
    days_of_week: Mapped[list | None] = mapped_column(
        JSONB, nullable=True,
        comment='e.g. ["mon", "wed", "fri"] — null means every day'
    )
    start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Optional: special becomes active from this date"
    )
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Optional: special expires after this date"
    )

    # ── Rules ────────────────────────────────────────────────────────────
    rule_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment='Flexible rules: {"type": "bogo", "buy": 2, "get": 1, "item_id": "..."}'
    )

    # ── Display ──────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
