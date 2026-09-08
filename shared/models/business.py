"""
Business model — core tenant entity.
Every business-owned resource is scoped through business_id FK.

Model 1: whatsapp_access_token_encrypted REMOVED.
  Platform uses ONE System User Access Token from env (WHATSAPP_DEFAULT_ACCESS_TOKEN).
  Each business only needs whatsapp_phone_number_id for routing.
"""

import uuid
from datetime import datetime, timezone

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
    # PayFast recurring-billing token — the PayFast-provider equivalent of
    # stripe_subscription_id, populated once the owner completes a PayFast
    # subscription checkout (backend/app/billing/payfast_billing.py).
    payfast_subscription_token: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )

    # ── Limits (enforced server-side) ────────────────────────────────────
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=800, nullable=False)
    daily_llm_call_limit: Mapped[int] = mapped_column(Integer, default=400, nullable=False)
    daily_order_limit: Mapped[int] = mapped_column(Integer, default=200, nullable=False)

    # ── Monthly order quota / packs (NextGen Restaurant plan, added 2026-07-24) ─
    # "1,500 Included Customer Orders per Month" — a *billing-period* quota,
    # distinct from daily_order_limit above (a same-day abuse/runaway-cost
    # guard). Usage is computed from the existing DailyUsage.orders_created
    # signal (see backend/app/billing/usage.py), not a separately-maintained
    # counter, so it can never drift from what actually happened.
    monthly_order_quota: Mapped[int] = mapped_column(Integer, default=1500, nullable=False)
    order_pack_credits: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Extra orders purchased on top of monthly_order_quota via an Order Pack add-on"
    )
    billing_period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="Reset to now() when a subscription invoice is paid (stripe_webhooks.py) — "
                 "defines the window monthly_order_quota/order_pack_credits apply to"
    )

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
    # ── POS discount limit (Phase 1) ─────────────────────────────────────
    max_staff_discount_cents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Max discount a STAFF/MANAGER can apply on a POS order without "
                 "owner sign-off; 0 = no staff discretion. OWNER is never limited."
    )
    # ── POS till payment methods (Phase 2) ───────────────────────────────
    # Distinct from payment_methods_enabled above, which governs the
    # WhatsApp/online-order payment flow. These control what a cashier sees
    # as buttons on the till's payment screen. CARD covers any physical card
    # terminal (Yoco/iKhokha/etc.) confirmed manually by the cashier — the
    # individual provider flags exist for future terminal-integration work,
    # not because the till shows 7 separate buttons today.
    pos_cash_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pos_card_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pos_payfast_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_yoco_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_ikhoka_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_direct_eft_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_other_payment_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pos_table_number_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="If true, the till requires a table number for DINE_IN orders"
    )

    # ── VAT / tax (Ciao Pizza P0) ────────────────────────────────────────
    # Stored in basis points so 15% is 1500 and no float ever touches a
    # tax figure. A VAT-registered SA business must show its VAT number and
    # a tax breakdown on the slip for it to be a valid tax invoice.
    vat_registered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vat_rate_basis_points: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="15% VAT = 1500 basis points"
    )
    vat_inclusive_pricing: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="True (the SA norm): menu prices already include VAT, which is "
                "back-calculated out of the total. False: VAT is added on top."
    )

    # ── Receipt / thermal printing (Ciao Pizza P0) ───────────────────────
    receipt_paper_width_mm: Mapped[int] = mapped_column(
        Integer, default=80, nullable=False,
        comment="58 or 80 — drives both the print CSS and ESC/POS column count"
    )
    receipt_auto_print: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Print the customer receipt automatically on checkout"
    )
    receipt_copies: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    print_kitchen_receipt: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Also print a kitchen slip on checkout for items needing prep"
    )
    receipt_config_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Tenant receipt template config — trading/franchise name, branch, "
                "address, phone, footer, QR, header lines. See backend/app/receipts/."
    )
    menu_reference_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Photo/scan/PDF of the client's EXISTING menu, kept as an "
                "onboarding reference for assisted import. Never shown to a "
                "customer — the live menu is always the structured records."
    )
    receipt_reference_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Photo/scan/PDF of the client's CURRENT till slip, kept as an "
                "onboarding design reference. Never printed — the live receipt is "
                "always rendered from receipt_config_json."
    )

    # ── Inventory module (Ciao Pizza P0) ─────────────────────────────────
    inventory_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Master switch. When false the POS behaves exactly as it did "
                "before the inventory module existed — no recipe resolution, no "
                "stock movements. Existing tenants are unaffected by default."
    )
    wastage_approval_threshold_cents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Wastage valued above this needs manager approval; 0 = never"
    )

    # ── Staff app navigation (per-tenant, NOT per-platform) ──────────────
    # These hide TABS, never features. A tenant that hides the wastage tab
    # keeps the wastage route, its API, its manager reporting and all of its
    # history — the feature is untouched platform-wide, this only decides
    # what earns one of the five slots on a phone-width bottom bar.
    staff_live_orders_tab_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Show the Live Orders tab in the staff app. A till-only tenant "
                "with no WhatsApp ordering has nothing to watch there, since a "
                "POS sale is already paid and completed when it is rung up. "
                "Defaults TRUE so no existing tenant loses the tab; this is a "
                "deliberate per-tenant choice rather than being derived from "
                "is_whatsapp_enabled, which governs the BOT, not the staff UI."
    )
    staff_wastage_tab_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Show the Wastage tab in the staff app's bottom navigation. "
                "Hiding it does NOT disable wastage — the page, API and "
                "manager reports all continue to work."
    )
    staff_stocktake_tab_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Show the Stocktake tab in the staff app's bottom navigation. "
                "Only meaningful when inventory_enabled is true; the API "
                "enforces that independently of this display flag."
    )

    # ── Stocktake schedule ───────────────────────────────────────────────
    # How often the owner expects a count. This is guidance shown to staff and
    # used to flag an overdue count — it never blocks a count, because a
    # restaurant that needs to recount at 3pm must always be able to.
    stocktake_frequency: Mapped[str] = mapped_column(
        String(16), default="DAILY_1", nullable=False,
        comment="DAILY_1 | DAILY_2 | DAILY_3 | DAILY_4 | CUSTOM"
    )
    stocktake_times_json: Mapped[list | None] = mapped_column(
        JSONB, nullable=True,
        comment="Local 'HH:MM' times a count is expected. Required when "
                "stocktake_frequency is CUSTOM; for DAILY_n it holds the n "
                "chosen times so the owner can move them."
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
