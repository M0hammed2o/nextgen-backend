"""
Business model — core tenant entity.
Every business-owned resource is scoped through business_id FK.

Model 1: whatsapp_access_token_encrypted REMOVED.
  Platform uses ONE System User Access Token from env (WHATSAPP_DEFAULT_ACCESS_TOKEN).
  Each business only needs whatsapp_phone_number_id for routing.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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
    # logo_asset_id has existed since 0001_initial_schema but lay dormant
    # (nothing read or wrote it) until the UI V2 programme wired it up. It is
    # reused rather than replaced — see migration 0053 for why a second logo
    # column was deliberately NOT added.
    logo_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Restaurant logo in `assets` (kind BUSINESS_LOGO). Resolved to a "
                "displayable URL via routes_assets.asset_public_url()."
    )
    brand_primary_colour: Mapped[str | None] = mapped_column(
        String(9), nullable=True,
        comment="Primary brand colour as #RRGGBB. NULL = NextGen default theme. "
                "Derived variants are computed at runtime, never stored."
    )
    brand_accent_colour: Mapped[str | None] = mapped_column(
        String(9), nullable=True,
        comment="Optional secondary accent as #RRGGBB. NULL = derived from primary."
    )
    brand_icon_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Square icon/monogram source in `assets`. Separate from logo_asset_id "
                "because a wide wordmark cannot be cropped square without distorting it."
    )
    brand_display_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="Short trading name shown in the UI. Falls back to `name`, which stays "
                "the legal/registered name used on NextGen's commercial invoices."
    )
    brand_theme: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="LIGHT",
        comment="LIGHT | DARK | SYSTEM."
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

    # ── Digital receipts (Commercial V1 gate, migration 0039) ────────────
    # Master switch, same pattern as inventory_enabled. Gates the WhatsApp
    # delivery channel of a digital receipt ONLY — printing (POS till slip)
    # is never gated by this. See backend/app/receipts/whatsapp_receipt.py.
    digital_receipts_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Master switch for the WhatsApp channel of digital receipts. "
                "Printing is always available regardless of this flag."
    )
    whatsapp_receipt_template_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Per-business override of settings.RECEIPT_WHATSAPP_TEMPLATE_NAME "
                "— must match an ACTUALLY Meta-approved template name for the "
                "sending WABA (platform token or this business's own connection)."
    )
    whatsapp_receipt_template_language: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="Per-business override of settings.RECEIPT_WHATSAPP_TEMPLATE_LANGUAGE "
                "— a Meta template language code, e.g. 'en_US'."
    )
    # ── POS Pay by Link (migration 0055) — same pattern as the receipt pair
    # above: a platform default (settings.POS_PAYMENT_LINK_TEMPLATE_NAME/
    # _LANGUAGE) with an optional per-business override here. Whether any
    # such template is actually Meta-approved for a given WABA is NOT
    # verified by this column's existence — see
    # backend/app/payments/pos_link_whatsapp.py for the honest
    # TEMPLATE_REQUIRED failure path when it isn't.
    whatsapp_payment_link_template_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="Per-business override of settings.POS_PAYMENT_LINK_TEMPLATE_NAME."
    )
    whatsapp_payment_link_template_language: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="Per-business override of settings.POS_PAYMENT_LINK_TEMPLATE_LANGUAGE."
    )

    # ── Delivery marketplace integrations (migration 0044) ───────────────
    # NOT the same thing as `delivery_enabled` above, which governs whether
    # the WhatsApp bot offers the restaurant's OWN-driver delivery — kept
    # deliberately separate names to avoid colliding with it (see
    # docs/DELIVERY_PLATFORM_ARCHITECTURE.md §0.3). Platform-admin-writable,
    # business-plane READ-ONLY: recommended SUPER_ADMIN-only for v1 (not
    # added to the ONBOARDER business-patch allow-list) since flipping a
    # live delivery integration on carries real contractual weight with the
    # provider that a sales partner shouldn't be able to trigger unilaterally.
    # No admin-suite toggle route was added in this change — see TODO.md.
    delivery_platforms_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Master switch for the delivery marketplace module as a "
                "whole (same pattern as inventory_enabled). Both this AND "
                "the specific provider flag below must be true for that "
                "provider's webhook to be actioned rather than denied.",
    )
    uber_eats_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="This tenant's Uber Eats connection is entitled to go live.",
    )
    mrd_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Reserved. MrDAdapter is a disabled stub in this release — "
                "no live Mr D API integration exists yet (see "
                "backend/app/delivery/mrd/adapter.py) — this flag has no "
                "effect today beyond being readable/settable.",
    )

    # ── Delivery reconciliation entitlement (migration 0054) ─────────────
    # A DELIBERATELY SEPARATE concept from delivery_platforms_enabled above.
    # That flag means commercial access to the LIVE marketplace integrations
    # (Uber Eats direct order injection, Mr D connection). This flag means
    # commercial access to the older CSV/manual delivery-order reconciliation
    # feature (routes_external_orders.py's preview/commit/batches/mappings),
    # which has always been baseline functionality with no entitlement gate
    # at all. A tenant can have either, both, or neither — never conflate the
    # two in frontend or schema logic (see UI V2.1 follow-up DECISIONS.md
    # entry and PRODUCT_MAP.md).
    #
    # Platform-admin-writable, business-plane READ-ONLY, same pattern as
    # every other Commercial V1 entitlement column. Defaults FALSE for every
    # tenant, including existing CSV-importer users — deliberately NOT
    # backfilled TRUE like loyalty_enabled was, because there is no reliable
    # existing on/off signal to backfill from (see migration 0054's
    # docstring). The backend import routes themselves do NOT yet enforce
    # this column — it is model/schema/nav-visibility only in this change;
    # enforcing it at the API layer is a deliberate follow-up once Admin has
    # explicitly granted it to every tenant who currently relies on the
    # importer, not something this migration should flip unilaterally.
    delivery_reconciliation_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Commercial entitlement for CSV/manual delivery-order "
                "reconciliation (Uber Eats, Mr D, generic platforms), "
                "separate from delivery_platforms_enabled's live-marketplace "
                "meaning. Not yet enforced by routes_external_orders.py — "
                "nav-visibility/schema only as of migration 0054.",
    )

    # ── Advanced Analytics entitlement (Commercial V1 gate, migration 0047) ──
    # Master switch, same pattern as inventory_enabled/digital_receipts_enabled/
    # delivery_platforms_enabled. NextGen POS gets Core Analytics (order counts,
    # revenue, top items, daily trend) ungated. NextGen Complete and above also
    # gets Advanced Analytics: the delivery-channel breakdown
    # (GET /business/external-orders/channels), average order value
    # (GET /business/analytics/average-order-value), and the cash/card payment-
    # method breakdown (GET /business/analytics/payment-methods). Platform-
    # admin-writable, business-plane READ-ONLY (see backend routes_business.py).
    advanced_analytics_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Master switch for Advanced Analytics (NextGen Complete tier "
                "and above). When false, gated analytics endpoints return "
                "409 ADVANCED_ANALYTICS_NOT_ENABLED. Core analytics (order "
                "counts, revenue, top items, daily trend) are never gated."
    )

    # ── Loyalty entitlement (Commercial V1 gate, migration 0051) ─────────
    # Master switch, same pattern as inventory_enabled/digital_receipts_enabled/
    # delivery_platforms_enabled/advanced_analytics_enabled. Closes
    # KNOWN_BUGS.md BUG-0107: before this column existed, the only gate on
    # Loyalty was LoyaltyProgram.is_enabled, which any OWNER/MANAGER could
    # flip on themselves via PUT /business/loyalty/program with no
    # package-tier check at all. Platform-admin-writable, business-plane
    # READ-ONLY (see backend routes_business.py). Migration 0051 backfills
    # this TRUE for any business with an already-enabled LoyaltyProgram at
    # the time it shipped, so no existing user of the feature lost it —
    # see that migration's docstring for the full reasoning.
    loyalty_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Master switch for the Loyalty module (NextGen Complete tier "
                "and above). When false, every route in routes_loyalty.py "
                "and POS checkout's loyalty_reward_id redemption refuse with "
                "409 LOYALTY_NOT_ENABLED. Points EARNING on a paid order is "
                "deliberately NOT gated by this column — see "
                "backend/app/loyalty/engine.py::award_points_for_paid_order "
                "and DECISIONS.md for why."
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

    # ── Sales-partner onboarding (Milestone B) ───────────────────────────
    # Set once, at creation, when an ONBOARDER (not SUPER_ADMIN) admin
    # created this business — see admin_api/app/core/deps.py::get_business_scoped.
    # NULL for every business created directly by a SUPER_ADMIN. ON DELETE
    # SET NULL rather than CASCADE/RESTRICT: removing the admin account that
    # onboarded a tenant must never delete or block deleting real
    # restaurant data over an admin-plane housekeeping action.
    onboarded_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Stamped by POST /admin/businesses/{id}/onboarding/complete. Once set,
    # the ONBOARDER who created this business loses all access to it
    # (get_business_scoped denies with 404) — SUPER_ADMIN access is
    # unaffected either way.
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Commercial trials & invoicing (Wave 0A) ───────────────────────────
    # billing_status="TRIAL"/trial_ends_at (above) remain the single stored
    # facts about trial state — these are ADDITIONAL, purely additive fields
    # that don't duplicate or risk disagreeing with them. See
    # backend/app/billing/account_status.py for the one function that
    # derives a displayed commercial status from all of these together.
    trial_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When the current/most recent trial began.",
    )
    converted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When this business first paid (billing_status flipped to "
                "ACTIVE via a paid invoice/subscription charge). Set once, "
                "never cleared.",
    )
    trial_extension_days_total: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Cumulative days this trial has been extended by. Caps an "
                "ONBOARDER's own extensions at ONBOARDER_MAX_TRIAL_EXTENSION_DAYS "
                "(config) — SUPER_ADMIN is unlimited.",
    )
    onboarding_confirmations: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False,
        comment="Manual go-live confirmations an admin recorded because the "
                "system cannot observe them automatically (e.g. "
                "{'modules': {'confirmed_by': <admin_id>, 'confirmed_at': "
                "<iso8601>, 'note': ...}}) — see POST "
                "/admin/businesses/{id}/onboarding/confirm.",
    )

    # ── Relationships (lazy loaded by default) ───────────────────────────
    users = relationship("BusinessUser", back_populates="business", lazy="selectin")
    menu_categories = relationship("MenuCategory", back_populates="business", lazy="noload")
    menu_items = relationship("MenuItem", back_populates="business", lazy="noload")

    def __repr__(self) -> str:
        return f"<Business {self.name} ({self.business_code})>"
