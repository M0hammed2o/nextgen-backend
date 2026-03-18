"""
Admin business management — CRUD, suspend/unsuspend, set limits,
create owner login, WhatsApp test send.
Only accessible by SUPER_ADMIN.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser, require_super_admin
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from shared.models.business import Business
from shared.models.user import BusinessUser
from shared.utils import generate_business_code

logger = logging.getLogger("nextgen.admin")
router = APIRouter(prefix="/admin/businesses", tags=["admin-businesses"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class BusinessCreate(BaseModel):
    name: str = Field(max_length=255)
    slug: str | None = Field(default=None, max_length=255, pattern=r"^[a-z0-9\-]*$")
    timezone: str = "Africa/Johannesburg"
    plan: str = "STARTER"
    whatsapp_phone_number_id: str | None = None
    daily_message_limit: int = 800
    daily_llm_call_limit: int = 400
    daily_order_limit: int = 200
    # Optional: create owner user in the same transaction
    owner_email: EmailStr | None = None
    owner_password: str | None = Field(default=None, min_length=8, max_length=128)
    owner_full_name: str | None = Field(default=None, max_length=255)


class BusinessUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    plan: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    billing_status: str | None = None


class LimitsUpdate(BaseModel):
    daily_message_limit: int | None = Field(default=None, ge=0)
    daily_llm_call_limit: int | None = Field(default=None, ge=0)
    daily_order_limit: int | None = Field(default=None, ge=0)


class SuspendRequest(BaseModel):
    reason: str = Field(max_length=512)


class BusinessAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    is_active: bool
    suspended_reason: str | None
    timezone: str
    plan: str
    billing_status: str
    currency: str
    whatsapp_phone_number_id: str | None
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    last_webhook_received_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


class OwnerCreateRequest(BaseModel):
    """Create an owner/manager login for a business."""
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(max_length=255)
    role: str = Field(default="OWNER", pattern=r"^(OWNER|MANAGER)$")


class OwnerResponse(BaseModel):
    id: uuid.UUID
    email: str
    staff_name: str | None
    role: str
    business_id: uuid.UUID
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class WhatsAppTestSendRequest(BaseModel):
    """Send a test WhatsApp message to verify outbound works."""
    to: str = Field(
        description="Recipient phone number in international format, e.g. 27612345678",
        pattern=r"^\d{10,15}$",
    )
    text: str = Field(
        default="Hello from NextGen test 🚀",
        max_length=4096,
    )


class WhatsAppTestSendResponse(BaseModel):
    success: bool
    wa_message_id: str | None = None
    error: str | None = None
    phone_number_id: str


# ── Existing CRUD Routes (unchanged) ────────────────────────────────────────

@router.get("", response_model=list[BusinessAdminResponse])
async def list_businesses(
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(Business)
    if is_active is not None:
        query = query.where(Business.is_active == is_active)
    query = query.order_by(Business.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    return [BusinessAdminResponse.model_validate(b) for b in result.scalars().all()]


@router.post("", response_model=BusinessAdminResponse, status_code=201)
async def create_business(
    body: BusinessCreate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    # Auto-generate slug from name if not provided
    import re as _re
    slug = body.slug
    if not slug:
        slug = _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
        if not slug:
            slug = "business"

    # Ensure slug uniqueness — append suffix if needed
    base_slug = slug
    for suffix in range(20):
        candidate = base_slug if suffix == 0 else f"{base_slug}-{suffix}"
        existing = await db.execute(
            select(Business.id).where(Business.slug == candidate)
        )
        if not existing.scalar_one_or_none():
            slug = candidate
            break
    else:
        raise AppError("SLUG_GENERATION_FAILED", "Could not generate unique slug", 500)
    body_slug = slug

    # If owner_email provided, check email uniqueness
    if body.owner_email:
        email_check = await db.execute(
            select(BusinessUser.id).where(
                BusinessUser.email == body.owner_email.lower().strip()
            )
        )
        if email_check.scalar_one_or_none():
            raise DuplicateError("User", "email")

        if not body.owner_password:
            raise AppError(
                "MISSING_PASSWORD",
                "owner_password is required when owner_email is provided",
                422,
            )

    # Generate unique business code
    for _ in range(10):
        code = generate_business_code()
        check = await db.execute(
            select(Business.id).where(Business.business_code == code)
        )
        if not check.scalar_one_or_none():
            break
    else:
        raise AppError("CODE_GENERATION_FAILED", "Could not generate unique business code", 500)

    # Create business
    biz_data = body.model_dump(exclude={"owner_email", "owner_password", "owner_full_name", "slug"})
    biz = Business(business_code=code, slug=body_slug, **biz_data)
    db.add(biz)
    await db.flush()  # Get biz.id before creating user

    # Optionally create owner user in same transaction
    if body.owner_email and body.owner_password:
        owner = BusinessUser(
            business_id=biz.id,
            role="OWNER",
            email=body.owner_email.lower().strip(),
            password_hash=hash_password(body.owner_password),
            staff_name=body.owner_full_name,
            is_active=True,
        )
        db.add(owner)

    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.get("/{business_id}", response_model=BusinessAdminResponse)
async def get_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))
    return BusinessAdminResponse.model_validate(biz)


@router.patch("/{business_id}", response_model=BusinessAdminResponse)
async def update_business(
    business_id: uuid.UUID,
    body: BusinessUpdate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(biz, field, value)
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/suspend", response_model=BusinessAdminResponse)
async def suspend_business(
    business_id: uuid.UUID,
    body: SuspendRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.is_active = False
    biz.suspended_reason = body.reason
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/unsuspend", response_model=BusinessAdminResponse)
async def unsuspend_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.is_active = True
    biz.suspended_reason = None
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/limits", response_model=BusinessAdminResponse)
async def set_business_limits(
    business_id: uuid.UUID,
    body: LimitsUpdate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(biz, field, value)
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature 1 — Create owner/manager login for a business
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{business_id}/owner",
    response_model=OwnerResponse,
    status_code=201,
    summary="Create owner/manager login for a business",
)
async def create_business_owner(
    business_id: uuid.UUID,
    body: OwnerCreateRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a BusinessUser with OWNER or MANAGER role linked to the business.
    Uses email + password auth (same as POST /auth/login on backend).
    """
    # Check business exists
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    # Check email uniqueness (globally unique)
    email_lower = body.email.lower().strip()
    existing = await db.execute(
        select(BusinessUser.id).where(BusinessUser.email == email_lower)
    )
    if existing.scalar_one_or_none():
        raise DuplicateError("User", "email")

    owner = BusinessUser(
        business_id=business_id,
        role=body.role,
        email=email_lower,
        password_hash=hash_password(body.password),
        staff_name=body.full_name,
        is_active=True,
    )
    db.add(owner)
    await db.commit()
    await db.refresh(owner)

    return OwnerResponse.model_validate(owner)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature 2 — WhatsApp test message send
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{business_id}/whatsapp/test-send",
    response_model=WhatsAppTestSendResponse,
    summary="Send a WhatsApp test message (uses platform token)",
)
async def send_whatsapp_test(
    business_id: uuid.UUID,
    body: WhatsAppTestSendRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a text message via WhatsApp to verify outbound works.
    Uses WHATSAPP_DEFAULT_ACCESS_TOKEN from env (Model 1).
    """
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    if not biz.whatsapp_phone_number_id:
        raise AppError(
            "NO_PHONE_NUMBER",
            "Business does not have a whatsapp_phone_number_id configured",
            422,
        )

    if not biz.is_whatsapp_enabled:
        raise AppError(
            "WHATSAPP_DISABLED",
            "WhatsApp is disabled for this business",
            422,
        )

    # Use the low-level sender (no DB persistence for test messages)
    from backend.app.bot.whatsapp_sender import _send_via_meta_api_with_detail
    from backend.app.core.config import get_settings as _get_backend_settings

    _backend_settings = _get_backend_settings()
    _masked = lambda t: f"{t[:6]}...{t[-4:]}" if len(t) > 12 else "***"  # noqa: E731

    phone_number_id = biz.whatsapp_phone_number_id
    token_source = "env:WHATSAPP_DEFAULT_ACCESS_TOKEN"
    token = _backend_settings.WHATSAPP_DEFAULT_ACCESS_TOKEN

    logger.info(
        "WhatsApp test-send: business_id=%s, phone_number_id=%s, token_source=%s, token=%s, to=%s",
        business_id,
        phone_number_id,
        token_source,
        _masked(token),
        body.to,
    )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": body.to,
        "type": "text",
        "text": {"body": body.text},
    }

    wamid, error_detail = await _send_via_meta_api_with_detail(
        phone_number_id=phone_number_id,
        recipient_wa_id=body.to,
        payload=payload,
    )

    if wamid:
        logger.info(
            "Test message sent: business=%s, to=%s, wamid=%s",
            business_id, body.to, wamid,
        )
        return WhatsAppTestSendResponse(
            success=True,
            wa_message_id=wamid,
            phone_number_id=phone_number_id,
        )
    else:
        logger.error(
            "Test message failed: business=%s, phone_number_id=%s, error=%s",
            business_id, phone_number_id, error_detail,
        )
        return WhatsAppTestSendResponse(
            success=False,
            error=error_detail or "Meta API call failed — check logs for details",
            phone_number_id=phone_number_id,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature — Billing PDF / Invoice generation
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{business_id}/billing/pdf",
    summary="Generate billing PDF summary for a business",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def generate_billing_pdf(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and download a billing summary PDF for a business.
    Includes business details, plan, limits, and usage summary if available.
    """
    from fastapi.responses import Response as FastAPIResponse
    import io

    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    # Try to load current month's usage
    usage_data = None
    try:
        from shared.models.analytics import DailyUsage
        from datetime import date, timedelta
        today = date.today()
        first_of_month = today.replace(day=1)
        result = await db.execute(
            select(DailyUsage).where(
                DailyUsage.business_id == business_id,
                DailyUsage.day >= first_of_month,
                DailyUsage.day <= today,
            )
        )
        rows = list(result.scalars().all())
        if rows:
            usage_data = {
                "inbound_messages": sum(r.inbound_messages for r in rows),
                "outbound_messages": sum(r.outbound_messages for r in rows),
                "llm_calls": sum(r.llm_calls for r in rows),
                "llm_tokens": sum(r.llm_tokens for r in rows),
                "orders_created": sum(r.orders_created for r in rows),
                "revenue_cents": sum(r.revenue_cents for r in rows),
            }
    except Exception:
        pass  # Usage data is optional

    # Generate PDF
    try:
        from fpdf import FPDF
    except ImportError:
        raise AppError("PDF_UNAVAILABLE", "PDF generation library not installed. Run: pip install fpdf2", 500)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Header
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(30, 64, 175)
    pdf.cell(0, 12, "NextGen AI Platform", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Billing Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Date
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"Generated: {datetime.now(timezone.utc).strftime('%d %B %Y at %H:%M UTC')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Separator
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Business Details section
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Business Details", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    def row(label: str, value: str):
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(55, 6, label, new_x="RIGHT")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    row("Business Name:", biz.name)
    row("Business Code:", biz.business_code)
    row("Slug:", biz.slug)
    row("Timezone:", biz.timezone)
    row("Currency:", biz.currency)
    row("Status:", "Active" if biz.is_active else f"Suspended — {biz.suspended_reason or 'N/A'}")
    pdf.ln(4)

    # Plan & Billing
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Plan & Billing", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    row("Plan:", biz.plan)
    row("Billing Status:", biz.billing_status)
    row("Stripe Customer:", biz.stripe_customer_id or "Not connected")
    row("Stripe Subscription:", biz.stripe_subscription_id or "Not connected")
    pdf.ln(4)

    # Limits
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Daily Limits", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    row("Daily Messages:", str(biz.daily_message_limit))
    row("Daily LLM Calls:", str(biz.daily_llm_call_limit))
    row("Daily Orders:", str(biz.daily_order_limit))
    pdf.ln(4)

    # Usage (if available)
    if usage_data:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 8, f"Usage Summary (Month to Date)", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        row("Inbound Messages:", str(usage_data["inbound_messages"]))
        row("Outbound Messages:", str(usage_data["outbound_messages"]))
        row("LLM Calls:", str(usage_data["llm_calls"]))
        row("LLM Tokens:", f"{usage_data['llm_tokens']:,}")
        row("Orders Created:", str(usage_data["orders_created"]))
        revenue_zar = usage_data["revenue_cents"] / 100
        row("Revenue:", f"R {revenue_zar:,.2f}")
        pdf.ln(4)

    # Footer
    pdf.ln(8)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "NextGen Intelligence (Pty) Ltd — Durban, KwaZulu-Natal, South Africa", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "info@nextgenintelligence.co.za | 083 786 6021", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "This is an internally generated billing summary, not a tax invoice.", new_x="LMARGIN", new_y="NEXT")

    # Output
    pdf_bytes = pdf.output()
    filename = f"nextgen-billing-{biz.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"

    return FastAPIResponse(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
