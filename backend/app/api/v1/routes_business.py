"""
Business settings routes — GET/PUT /business/settings
Only accessible by OWNER or MANAGER.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.crypto import decrypt_credential, encrypt_credential
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.ownership import require_owned
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.payments.registry import get_provider
from shared.enums import AuditScope
from shared.models.asset import Asset
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.user import BusinessUser

# Body fields that reference an Asset row by id — every one of them must be
# ownership-checked before being written to Business, the same principle
# routes_menu.py::_resolve_item_image already applies to menu item images.
_ASSET_REFERENCE_FIELDS = ("receipt_reference_asset_id", "menu_reference_asset_id")

router = APIRouter(prefix="/business", tags=["business"])

# Every provider name the registry actually knows how to serve — kept as the
# single source of truth for the `payment_provider` field's allowed values,
# so this constraint and the registry can never silently drift apart
# (BUG-0006: this field previously had no validation at all).
_VALID_PAYMENT_PROVIDERS = r"^(DIRECT_EFT|MOCK|YOCO|PAYFAST|STITCH|IKHOKA)$"

# Money-routing fields: a MANAGER changing any of these could redirect where
# the restaurant's payments end up (own bank account, own payment-provider
# API keys). OWNER-only; a MANAGER attempt is a 403 naming the field. Every
# other BusinessSettingsUpdate field is unchanged for managers.
_OWNER_ONLY_MONEY_FIELDS = (
    "payment_provider",
    "payment_api_key",
    "payment_api_secret",
    "payment_webhook_secret",
    "eft_bank_name",
    "eft_account_name",
    "eft_account_number",
    "eft_branch_code",
)


# ── Schemas ──────────────────────────────────────────────────────────────────

class BusinessSettingsResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    timezone: str
    business_hours: dict | None
    greeting_text: str | None
    fallback_text: str | None
    closed_text: str | None
    busy_text: str | None
    order_in_only: bool
    delivery_enabled: bool
    delivery_fee_cents: int
    require_customer_name: bool
    require_phone_number: bool
    require_delivery_address: bool
    currency: str
    plan: str
    billing_status: str
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    # Monthly order quota (NextGen Restaurant plan) — distinct from the
    # daily_order_limit runaway-cost guard above.
    monthly_order_quota: int = 1500
    order_pack_credits: int = 0
    monthly_orders_used: int = 0
    monthly_orders_remaining: int = 1500
    address: str | None
    phone: str | None
    menu_image_url: str | None
    # Payment settings
    payment_methods_enabled: list | None = None
    online_payment_required: bool = False
    payment_provider: str | None = None
    payment_timeout_minutes: int = 30
    eft_bank_name: str | None = None
    eft_account_name: str | None = None
    eft_account_number: str | None = None
    eft_branch_code: str | None = None
    # VAT / receipt / inventory (Ciao Pizza P0)
    vat_registered: bool = False
    vat_number: str | None = None
    vat_rate_basis_points: int = 0
    vat_inclusive_pricing: bool = True
    receipt_paper_width_mm: int = 80
    receipt_auto_print: bool = True
    receipt_copies: int = 1
    print_kitchen_receipt: bool = False
    receipt_config_json: dict | None = None
    receipt_reference_asset_id: uuid.UUID | None = None
    menu_reference_asset_id: uuid.UUID | None = None
    inventory_enabled: bool = False
    # Platform-admin kill switch (Business.is_whatsapp_enabled) — read-only
    # here so the manager dashboard can hide WhatsApp sections for a
    # POS-only client. Not settable via BusinessSettingsUpdate: enabling/
    # disabling WhatsApp is a platform decision made through the admin
    # plane, not something a tenant switches on themselves (same rule
    # already applied to inventory_enabled above).
    is_whatsapp_enabled: bool = True
    wastage_approval_threshold_cents: int = 0
    # Staff app navigation + stocktake schedule (per-tenant configuration)
    staff_live_orders_tab_enabled: bool = True
    staff_wastage_tab_enabled: bool = True
    staff_stocktake_tab_enabled: bool = False
    stocktake_frequency: str = "DAILY_1"
    stocktake_times_json: list | None = None
    eft_reference_prefix: str | None = None
    # Credential hints — never return raw keys, only show last-4 masked
    payment_api_key_hint: str | None = None
    payment_api_secret_hint: str | None = None
    payment_webhook_secret_configured: bool = False
    # POS till payment settings — distinct from payment_methods_enabled
    # above, which governs the WhatsApp/online-order flow.
    pos_cash_enabled: bool = True
    pos_card_enabled: bool = True
    pos_payfast_enabled: bool = False
    pos_yoco_enabled: bool = False
    pos_ikhoka_enabled: bool = False
    pos_direct_eft_enabled: bool = False
    pos_other_payment_enabled: bool = False
    pos_table_number_required: bool = False

    model_config = {"from_attributes": False}

    @classmethod
    def from_business(cls, b: "Business") -> "BusinessSettingsResponse":
        def _hint(val: str | None) -> str | None:
            if not val:
                return None
            return "****" + val[-4:] if len(val) >= 4 else "****"

        return cls(
            id=b.id,
            name=b.name,
            slug=b.slug,
            business_code=b.business_code,
            timezone=b.timezone,
            business_hours=b.business_hours,
            greeting_text=b.greeting_text,
            fallback_text=b.fallback_text,
            closed_text=b.closed_text,
            busy_text=b.busy_text,
            order_in_only=b.order_in_only,
            delivery_enabled=b.delivery_enabled,
            delivery_fee_cents=b.delivery_fee_cents,
            require_customer_name=b.require_customer_name,
            vat_registered=b.vat_registered,
            vat_number=b.vat_number,
            vat_rate_basis_points=b.vat_rate_basis_points,
            vat_inclusive_pricing=b.vat_inclusive_pricing,
            receipt_paper_width_mm=b.receipt_paper_width_mm,
            receipt_auto_print=b.receipt_auto_print,
            receipt_copies=b.receipt_copies,
            print_kitchen_receipt=b.print_kitchen_receipt,
            receipt_config_json=b.receipt_config_json,
            receipt_reference_asset_id=b.receipt_reference_asset_id,
            menu_reference_asset_id=b.menu_reference_asset_id,
            inventory_enabled=b.inventory_enabled,
            is_whatsapp_enabled=b.is_whatsapp_enabled,
            wastage_approval_threshold_cents=b.wastage_approval_threshold_cents,
            staff_live_orders_tab_enabled=b.staff_live_orders_tab_enabled,
            staff_wastage_tab_enabled=b.staff_wastage_tab_enabled,
            staff_stocktake_tab_enabled=b.staff_stocktake_tab_enabled,
            stocktake_frequency=b.stocktake_frequency,
            stocktake_times_json=b.stocktake_times_json,
            require_phone_number=b.require_phone_number,
            require_delivery_address=b.require_delivery_address,
            currency=b.currency,
            plan=b.plan,
            billing_status=b.billing_status,
            daily_message_limit=b.daily_message_limit,
            daily_llm_call_limit=b.daily_llm_call_limit,
            daily_order_limit=b.daily_order_limit,
            monthly_order_quota=b.monthly_order_quota,
            order_pack_credits=b.order_pack_credits,
            address=b.address,
            phone=b.phone,
            menu_image_url=b.menu_image_url,
            payment_methods_enabled=b.payment_methods_enabled,
            online_payment_required=b.online_payment_required,
            payment_provider=b.payment_provider,
            payment_timeout_minutes=b.payment_timeout_minutes,
            eft_bank_name=b.eft_bank_name,
            eft_account_name=b.eft_account_name,
            eft_account_number=b.eft_account_number,
            eft_branch_code=b.eft_branch_code,
            eft_reference_prefix=b.eft_reference_prefix,
            payment_api_key_hint=_hint(decrypt_credential(b.payment_api_key)),
            payment_api_secret_hint=_hint(decrypt_credential(b.payment_api_secret)),
            payment_webhook_secret_configured=bool(b.payment_webhook_secret),
            pos_cash_enabled=b.pos_cash_enabled,
            pos_card_enabled=b.pos_card_enabled,
            pos_payfast_enabled=b.pos_payfast_enabled,
            pos_yoco_enabled=b.pos_yoco_enabled,
            pos_ikhoka_enabled=b.pos_ikhoka_enabled,
            pos_direct_eft_enabled=b.pos_direct_eft_enabled,
            pos_other_payment_enabled=b.pos_other_payment_enabled,
            pos_table_number_required=b.pos_table_number_required,
        )


class BusinessSettingsUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    business_hours: dict | None = None
    greeting_text: str | None = None
    fallback_text: str | None = None
    closed_text: str | None = None
    busy_text: str | None = None
    order_in_only: bool | None = None
    delivery_enabled: bool | None = None
    delivery_fee_cents: int | None = Field(default=None, ge=0)
    require_customer_name: bool | None = None
    require_phone_number: bool | None = None
    require_delivery_address: bool | None = None
    address: str | None = None
    phone: str | None = None
    menu_image_url: str | None = None
    # Payment settings
    payment_methods_enabled: list | None = None
    online_payment_required: bool | None = None
    payment_provider: str | None = Field(default=None, pattern=_VALID_PAYMENT_PROVIDERS)
    payment_timeout_minutes: int | None = Field(default=None, ge=1, le=1440)
    eft_bank_name: str | None = None
    eft_account_name: str | None = None
    eft_account_number: str | None = None
    eft_branch_code: str | None = None
    eft_reference_prefix: str | None = None
    # Provider credentials — accepted in PUT, never returned in GET
    payment_api_key: str | None = None
    payment_api_secret: str | None = None
    payment_webhook_secret: str | None = None
    # POS till payment settings
    pos_cash_enabled: bool | None = None
    pos_card_enabled: bool | None = None
    pos_payfast_enabled: bool | None = None
    pos_yoco_enabled: bool | None = None
    pos_ikhoka_enabled: bool | None = None
    pos_direct_eft_enabled: bool | None = None
    pos_other_payment_enabled: bool | None = None
    pos_table_number_required: bool | None = None
    # VAT / receipt config (Ciao Pizza P0). inventory_enabled is deliberately
    # NOT settable here — enabling a paid module is a platform decision, made
    # through the admin plane, not something a tenant switches on themselves.
    vat_registered: bool | None = None
    vat_number: str | None = Field(default=None, max_length=32)
    vat_rate_basis_points: int | None = Field(default=None, ge=0, le=10000)
    vat_inclusive_pricing: bool | None = None
    receipt_paper_width_mm: int | None = Field(default=None)
    receipt_auto_print: bool | None = None
    receipt_copies: int | None = Field(default=None, ge=1, le=5)
    print_kitchen_receipt: bool | None = None
    receipt_config_json: dict | None = None
    wastage_approval_threshold_cents: int | None = Field(default=None, ge=0)
    # Onboarding reference documents the restaurant uploads about itself.
    # Never printed or shown to a customer — see AssetKind.MENU_REFERENCE.
    receipt_reference_asset_id: uuid.UUID | None = None
    menu_reference_asset_id: uuid.UUID | None = None
    # Staff app navigation. These hide TABS, never features — the routes,
    # APIs and manager reporting behind each one are untouched, and no other
    # tenant is affected. Safe for an owner to set: it exposes nothing and
    # disables nothing.
    staff_live_orders_tab_enabled: bool | None = None
    staff_wastage_tab_enabled: bool | None = None
    staff_stocktake_tab_enabled: bool | None = None
    # How often the owner expects a count. Guidance and overdue-flagging
    # only — it never blocks staff from starting one.
    stocktake_frequency: str | None = Field(default=None, max_length=16)
    stocktake_times_json: list[str] | None = None

    @field_validator("stocktake_frequency")
    @classmethod
    def _known_frequency(cls, v: str | None) -> str | None:
        allowed = {"DAILY_1", "DAILY_2", "DAILY_3", "DAILY_4", "CUSTOM"}
        if v is not None and v not in allowed:
            raise ValueError(f"stocktake_frequency must be one of {sorted(allowed)}")
        return v

    @field_validator("stocktake_times_json")
    @classmethod
    def _valid_times(cls, v: list[str] | None) -> list[str] | None:
        # Stored as local 'HH:MM' strings. Validated here so a typo cannot
        # reach the overdue calculation and make every count look on-time.
        if v is None:
            return v
        if len(v) > 12:
            raise ValueError("at most 12 stocktake times")
        for t in v:
            parts = t.split(":")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise ValueError(f"invalid time {t!r} — expected HH:MM")
            hh, mm = int(parts[0]), int(parts[1])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError(f"invalid time {t!r} — expected HH:MM")
        return sorted(v)

    @field_validator("receipt_paper_width_mm")
    @classmethod
    def _supported_paper_width(cls, v: int | None) -> int | None:
        # A thermal roll is physically 58 or 80 mm; anything else would print
        # off the edge of the paper.
        if v is not None and v not in (58, 80):
            raise ValueError("receipt_paper_width_mm must be 58 or 80")
        return v


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=BusinessSettingsResponse)
async def get_business_settings(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Get current business settings."""
    result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    from backend.app.billing.usage import get_order_quota_status
    quota = await get_order_quota_status(db, business)

    response = BusinessSettingsResponse.from_business(business)
    response.monthly_orders_used = quota.used
    response.monthly_orders_remaining = quota.remaining
    return response


@router.put("/settings", response_model=BusinessSettingsResponse)
async def update_business_settings(
    body: BusinessSettingsUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Update business settings (partial update — only provided fields)."""
    update_data = body.model_dump(exclude_unset=True)

    # Money-routing fields are OWNER-only — checked before any DB lookup,
    # same pattern as the staff-management role gates, so a MANAGER attempt
    # never touches the row at all. Named in the error so the dashboard can
    # tell the manager exactly which field was rejected.
    if user.role != "OWNER":
        for field in _OWNER_ONLY_MONEY_FIELDS:
            if field in update_data:
                raise AppError(
                    "OWNER_ONLY_FIELD",
                    f"Only the business owner can change '{field}' — it controls where payments are routed",
                    403,
                )

    result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    # Exclude credential fields from the generic setattr loop —
    # they are handled separately so empty strings clear the stored value.
    _CREDENTIAL_FIELDS = {"payment_api_key", "payment_api_secret", "payment_webhook_secret"}
    changed_money_fields = [f for f in _OWNER_ONLY_MONEY_FIELDS if f in update_data]

    for field in _ASSET_REFERENCE_FIELDS:
        value = update_data.get(field)
        if value is not None:
            await require_owned(db, Asset, value, user.business_id, label="Asset")

    for field, value in update_data.items():
        if field in _CREDENTIAL_FIELDS:
            # Empty string → clear the credential; None (not set) → skip.
            # Credentials are encrypted at rest (Fernet, enc1: prefix).
            setattr(business, field, encrypt_credential(value) if value else None)
        else:
            setattr(business, field, value)

    # Validated against the RESULTING state rather than the request body: this
    # is a partial update, so an owner may set the frequency in one call and
    # the times in the next, and neither call sees the whole picture on its own.
    if business.stocktake_frequency == "CUSTOM" and not business.stocktake_times_json:
        raise AppError(
            "STOCKTAKE_TIMES_REQUIRED",
            "Choose at least one time of day for a custom stocktake schedule",
            422,
        )

    # Audit trail for money-routing changes — field names only, never the
    # credential/EFT values themselves (SECURITY.md: secrets never appear
    # in logs). Only OWNER can reach this point with a non-empty list.
    if changed_money_fields:
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="business.payment_settings.changed",
            target_type="business",
            target_id=business.id,
            diff_json={"fields_changed": changed_money_fields, "actor_role": user.role},
        ))

    await db.commit()
    await db.refresh(business)
    return BusinessSettingsResponse.from_business(business)


# ── Payment provider health check ────────────────────────────────────────────

class PaymentHealthResponse(BaseModel):
    healthy: bool
    provider: str
    checked_at: datetime
    detail: str | None = None


@router.get("/payments/health", response_model=PaymentHealthResponse)
async def get_payment_provider_health(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Readiness check for this business's configured payment provider — part
    of the Payment Provider Framework (see `backend/app/payments/base.py`).
    A configuration check (credentials present/well-formed), not a live API
    ping — see `PaymentProvider.check_health`'s docstring for why.
    """
    result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    if not business.payment_provider:
        return PaymentHealthResponse(
            healthy=False, provider="NONE", checked_at=datetime.now(timezone.utc),
            detail="No payment provider configured for this business",
        )

    provider = get_provider(business.payment_provider)
    if not provider:
        return PaymentHealthResponse(
            healthy=False, provider=business.payment_provider, checked_at=datetime.now(timezone.utc),
            detail="Configured provider name is not a recognized provider",
        )

    status = await provider.check_health(business)
    return PaymentHealthResponse(
        healthy=status.healthy, provider=status.provider, checked_at=status.checked_at, detail=status.detail,
    )


# ── WhatsApp pause/busy toggle ────────────────────────────────────────────────
# Staff-facing "too busy" pause — distinct from the platform-admin
# is_whatsapp_enabled kill switch. Any STAFF/MANAGER/OWNER can toggle it.

class WhatsAppStatusResponse(BaseModel):
    paused: bool
    paused_at: datetime | None = None
    paused_by_name: str | None = None
    busy_text: str | None = None


async def _load_business_for_toggle(db: AsyncSession, user: AuthUser) -> Business:
    result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    return business


async def _paused_by_name(db: AsyncSession, business: Business) -> str | None:
    if not business.whatsapp_paused or not business.whatsapp_paused_by_user_id:
        return None
    result = await db.execute(
        select(BusinessUser.staff_name).where(BusinessUser.id == business.whatsapp_paused_by_user_id)
    )
    return result.scalar_one_or_none()


async def _publish_whatsapp_status(business_id: uuid.UUID, event_type: str, paused_by_name: str | None) -> None:
    """Fire-and-forget SSE publish on the same channel used for order events."""
    try:
        import json

        from backend.app.db.session import get_redis
        redis = await get_redis()
        await redis.publish(
            f"orders:{business_id}",
            json.dumps({
                "type": event_type,
                "paused_by_name": paused_by_name,
                "at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception:
        pass


@router.get("/whatsapp/status", response_model=WhatsAppStatusResponse)
async def get_whatsapp_status(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Current pause state — used by the staff Profile screen and the manager banner."""
    business = await _load_business_for_toggle(db, user)
    return WhatsAppStatusResponse(
        paused=business.whatsapp_paused,
        paused_at=business.whatsapp_paused_at,
        paused_by_name=await _paused_by_name(db, business),
        busy_text=business.busy_text,
    )


@router.post("/whatsapp/pause", response_model=WhatsAppStatusResponse)
async def pause_whatsapp(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Pause incoming WhatsApp orders — customers get busy_text until resumed."""
    business = await _load_business_for_toggle(db, user)
    business.whatsapp_paused = True
    business.whatsapp_paused_at = datetime.now(timezone.utc)
    business.whatsapp_paused_by_user_id = user.user_id
    await db.commit()
    await db.refresh(business)

    staff_name = await _paused_by_name(db, business)

    await _publish_whatsapp_status(business.id, "whatsapp_paused", staff_name)
    try:
        from backend.app.push_service import send_whatsapp_status_alert
        import asyncio
        asyncio.create_task(
            send_whatsapp_status_alert(business_id=business.id, paused=True, staff_name=staff_name)
        )
    except Exception:
        pass

    return WhatsAppStatusResponse(
        paused=True,
        paused_at=business.whatsapp_paused_at,
        paused_by_name=staff_name,
        busy_text=business.busy_text,
    )


@router.post("/whatsapp/resume", response_model=WhatsAppStatusResponse)
async def resume_whatsapp(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Resume WhatsApp ordering."""
    business = await _load_business_for_toggle(db, user)
    business.whatsapp_paused = False
    business.whatsapp_paused_at = None
    business.whatsapp_paused_by_user_id = None
    await db.commit()
    await db.refresh(business)

    await _publish_whatsapp_status(business.id, "whatsapp_resumed", None)
    try:
        from backend.app.push_service import send_whatsapp_status_alert
        import asyncio
        asyncio.create_task(
            send_whatsapp_status_alert(business_id=business.id, paused=False, staff_name=None)
        )
    except Exception:
        pass

    return WhatsAppStatusResponse(paused=False, paused_at=None, paused_by_name=None, busy_text=business.busy_text)
