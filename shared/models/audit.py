"""
Audit events — platform-wide and per-business action logging.
Message outbox — reliable outbound message delivery pattern.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    """
    Unified audit log for both platform (admin) and business actions.
    scope='PLATFORM' for admin actions, scope='BUSINESS' for business-level.
    """
    __tablename__ = "audit_events"

    scope: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="PLATFORM or BUSINESS"
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="business_user.id or admin_user.id"
    )
    action: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="e.g. business.created, order.status_changed, menu_item.updated"
    )
    target_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="e.g. business, order, menu_item"
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    diff_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="What changed: {field: {old: ..., new: ...}}"
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Request correlation ID for tracing"
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


class MessageOutbox(Base, UUIDPrimaryKeyMixin):
    """
    Outbox pattern for reliable outbound WhatsApp message delivery.
    Background worker picks up pending messages, sends them, marks as sent/failed.
    Prevents message loss if Meta API is down during order creation.
    """
    __tablename__ = "message_outbox"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_wa_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Optional structured message payload (buttons, lists, etc.)"
    )

    # ── Delivery status ──────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", nullable=False,
        comment="PENDING / SENDING / SENT / FAILED"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_wa_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="WhatsApp message ID returned on successful send"
    )

    # ── Timestamps ───────────────────────────────────────────────────────
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
