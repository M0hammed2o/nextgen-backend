"""
Business model — core tenant entity.
Every business-owned resource is scoped through business_id FK.

Model 1: whatsapp_access_token_encrypted REMOVED.
  Platform uses ONE System User Access Token from env (WHATSAPP_DEFAULT_ACCESS_TOKEN).
  Each business only needs whatsapp_phone_number_id for routing.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Business(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "businesses"

    # ── Identity ─────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    business_code: Mapped[str] = mapped_column(
        String(6), unique=True, nullable=False,
        comment="6-char alphanumeric code for staff PIN login"
    )

    # ── Status ───────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── WhatsApp / Meta (Model 1: NO per-business token) ─────────────────
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True,
        comment="Meta phone_number_id — routes inbound webhooks to this business"
    )
    whatsapp_business_account_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Optional WABA ID for reference"
    )
    is_whatsapp_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Platform-admin kill switch. Toggle WhatsApp bot on/off without "
                 "removing phone_number_id. Authoritative over whatsapp_paused — "
                 "if False, WhatsApp stays off regardless of the staff pause flag."
    )
    last_webhook_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Staff busy/pause toggle (distinct from is_whatsapp_enabled) ──────
    whatsapp_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Staff-facing 'too busy' pause — blocks new inbound orders "
                 "with busy_text until a staff member resumes"
    )
    whatsapp_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    whatsapp_paused_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="business_users.id of the staff member who paused — not FK-enforced, "
                 "same pattern as Order.cancelled_by_user_id"
    )

    # ── Timezone & Hours ─────────────────────────────────────────────────
    timezone: Mapped[str] = mapped_column(
        String(64), default="Africa/Johannesburg", nullable=False
    )
    business_hours: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment='{"mon": {"open": "08:00", "close": "22:00"}, "sun": null}'
    )

    # ── Chatbot Texts ────────────────────────────────────────────────────
    greeting_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    busy_text: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Shown to customers while whatsapp_paused is true"
    )

    # ── Order Config ─────────────────────────────────────────────────────
    order_in_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delivery_fee_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    require_customer_name: Mapped[bool] = mapped_column(Boolean, default=False)
    require_phone_number: Mapped[bool] = mapped_column(Boolean, default=True)
    require_delivery_address: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Currency ─────────────────────────────────────────────────────────
    currency: Mapped[str] = mapped_column(String(3), default="ZAR", nullable=False)

    # ── Billing / Plan ───────────────────────────────────────────────────
    plan: Mapped[str] = mapped_column(String(32), default="STARTER", nullable=False)
    billing_status: Mapped[str] = mapped_column(
        String(32), default="TRIAL", nullable=False
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )

    # ── Limits (enforced server-side) ────────────────────────────────────
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=800, nullable=False)
    daily_llm_call_limit: Mapped[int] = mapped_column(Integer, default=400, nullable=False)
    daily_order_limit: Mapped[int] = mapped_column(Integer, default=200, nullable=False)

    # ── Order Numbering ──────────────────────────────────────────────────
    order_number_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Logo / Branding ──────────────────────────────────────────────────
    logo_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    menu_image_url: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Public URL of menu image to send via WhatsApp when customer requests menu"
    )
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── Payment Settings (Phase 9) ───────────────────────────────────────
    payment_methods_enabled: Mapped[list | None] = mapped_column(
        JSONB, nullable=True,
        comment='["CASH", "DIRECT_EFT", "PAYMENT_LINK"] — methods the business accepts'
    )
    online_payment_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="If true, order must be PAID before moving to IN_PROGRESS"
    )
    payment_provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="Active payment link provider: YOCO | PAYFAST | STITCH"
    )
    payment_timeout_minutes: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False,
        comment="Minutes before an UNPAID online-payment order is auto-cancelled"
    )
    # EFT banking details
    eft_bank_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    eft_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    eft_account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eft_branch_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    eft_reference_prefix: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="Prefix for payment references, e.g. 'BAR' → BAR-000123"
    )
    # Per-business payment provider credentials (Model B — each business owns their account)
    # Yoco:    api_key=Secret Key,  api_secret=<unused>,    webhook_secret=Webhook Secret
    # PayFast: api_key=Merchant Key, api_secret=Merchant ID, webhook_secret=Passphrase
    # Stitch:  api_key=Client Secret, api_secret=Client ID,  webhook_secret=<unused (uses api_key)>
    payment_api_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Primary provider credential — never returned in API responses"
    )
    payment_api_secret: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Secondary provider credential (PayFast merchant ID, Stitch client ID)"
    )
    payment_webhook_secret: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Webhook signing secret for verifying inbound payment webhooks"
    )

    # ── Relationships (lazy loaded by default) ───────────────────────────
    users = relationship("BusinessUser", back_populates="business", lazy="selectin")
    menu_categories = relationship("MenuCategory", back_populates="business", lazy="noload")
    menu_items = relationship("MenuItem", back_populates="business", lazy="noload")

    def __repr__(self) -> str:
        return f"<Business {self.name} ({self.business_code})>"
