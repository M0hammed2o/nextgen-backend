"""
whatsapp_connections — per-business, customer-owned WhatsApp connections
obtained through Meta Embedded Signup.

Wave 0A / Milestone C / DECISION-0022. This is ADDITIVE to the existing
Model 1 single-platform-token architecture (see DECISION-0001,
shared/models/business.py) — it does NOT replace it. A business with no row
here (or no CONNECTED row) keeps sending/receiving on the platform token,
exactly as before. See backend/app/whatsapp/token_resolver.py for the
fallback rule.

One connection per business (UNIQUE business_id) — a business owns at most
one customer WABA/number at a time in this design; reconnecting to a
different number replaces the row rather than adding a second one.

`access_token_encrypted`/`registration_pin_encrypted` are Fernet-encrypted
at rest (backend/app/core/crypto.py) and are wiped (set to None) on
disconnect. They must NEVER be included in any API response — see
routes_whatsapp_connect.py's response models, which deliberately omit them.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WhatsAppConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "whatsapp_connections"

    # ── Ownership ────────────────────────────────────────────────────────
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
        comment="One connection per business — reconnect REPLACES this row's fields"
    )

    # ── Meta identifiers (confirmed against the Graph API, never trusted
    #    blindly from what the browser's Embedded Signup callback claims) ──
    waba_id: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_number_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
        comment="Also mirrored onto businesses.whatsapp_phone_number_id for inbound routing"
    )
    meta_business_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Connection lifecycle ────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING",
        comment="PENDING | CONNECTED | NEEDS_ATTENTION | DISCONNECTED"
    )
    status_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Short, human-readable — e.g. which exchange step failed, or why "
                "Meta flagged the account. NEVER a token/secret/pin."
    )
    webhook_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    phone_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Credentials (Fernet-encrypted; wiped on disconnect) ─────────────
    access_token_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Business Integration System User token from the code exchange — "
                "Fernet 'enc1:' prefixed. NEVER returned in any API response or logged."
    )
    registration_pin_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Server-generated 6-digit PIN used for POST /{phone_number_id}/register — "
                "Fernet 'enc1:' prefixed. Stored so a re-register attempt can reuse it."
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Only set when Meta's token exchange response includes expires_in — "
                "a Business Integration System User token normally does not expire."
    )

    # ── Health / quality signals (from Graph reads, health-check, webhooks) ─
    quality_rating: Mapped[str | None] = mapped_column(String(16), nullable=True)
    messaging_limit_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────────────
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="business_users.id of the OWNER who completed the connect flow — "
                "not FK-enforced, same pattern as Order.cancelled_by_user_id"
    )

    def __repr__(self) -> str:
        return f"<WhatsAppConnection business={self.business_id} status={self.status}>"
