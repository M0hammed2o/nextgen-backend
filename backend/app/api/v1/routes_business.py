"""
Business settings routes — GET/PUT /business/settings
Only accessible by OWNER or MANAGER.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.crypto import decrypt_credential, encrypt_credential
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from shared.models.business import Business
from shared.models.user import BusinessUser

router = APIRouter(prefix="/business", tags=["business"])


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
    eft_reference_prefix: str | None = None
    # Credential hints — never return raw keys, only show last-4 masked
    payment_api_key_hint: str | None = None
    payment_api_secret_hint: str | None = None
    payment_webhook_secret_configured: bool = False

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
            require_phone_number=b.require_phone_number,
            require_delivery_address=b.require_delivery_address,
            currency=b.currency,
            plan=b.plan,
            billing_status=b.billing_status,
            daily_message_limit=b.daily_message_limit,
            daily_llm_call_limit=b.daily_llm_call_limit,
            daily_order_limit=b.daily_order_limit,
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
    payment_provider: str | None = None
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
    return BusinessSettingsResponse.from_business(business)


@router.put("/settings", response_model=BusinessSettingsResponse)
async def update_business_settings(
    body: BusinessSettingsUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Update business settings (partial update — only provided fields)."""
    result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    # Exclude credential fields from the generic setattr loop —
    # they are handled separately so empty strings clear the stored value.
    _CREDENTIAL_FIELDS = {"payment_api_key", "payment_api_secret", "payment_webhook_secret"}
    update_data = body.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        if field in _CREDENTIAL_FIELDS:
            # Empty string → clear the credential; None (not set) → skip.
            # Credentials are encrypted at rest (Fernet, enc1: prefix).
            setattr(business, field, encrypt_credential(value) if value else None)
        else:
            setattr(business, field, value)

    await db.commit()
    await db.refresh(business)
    return BusinessSettingsResponse.from_business(business)


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
