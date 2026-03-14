"""
Messages — inbound/outbound WhatsApp messages.
wa_message_id is globally unique for idempotency.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class Message(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "messages"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # ── WhatsApp Message ID (global unique for idempotency) ──────────────
    wa_message_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True,
        comment="wamid.* — used for deduplication"
    )

    direction: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="INBOUND / OUTBOUND"
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Full webhook payload or media metadata"
    )

    # ── LLM Tracking ─────────────────────────────────────────────────────
    is_llm: Mapped[bool] = mapped_column(
        default=False, nullable=False,
        comment="True if this message involved an LLM call"
    )
    llm_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="openai / anthropic / etc"
    )

    # ── Intent ───────────────────────────────────────────────────────────
    intent: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Detected intent from rules engine or LLM"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
