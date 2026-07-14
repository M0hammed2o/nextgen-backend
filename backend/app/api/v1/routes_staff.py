"""
Staff management routes — Owner/Manager can create, list, update, and deactivate staff users.
Includes PIN rotation and email+password creation for managers.
"""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.core.security import hash_password, hash_pin
from backend.app.db.session import get_db
from shared.models.user import BusinessUser, RefreshToken

router = APIRouter(prefix="/business/staff", tags=["staff"])


def _generate_pin() -> str:
    """4 digits — matches the staff till's fixed 4-digit PinKeypad UI."""
    return f"{secrets.randbelow(10000):04d}"


# ── Schemas ──────────────────────────────────────────────────────────────────

class StaffCreate(BaseModel):
    """
    Create a staff member (PIN-based) or manager (email+password).
    pin is optional for STAFF — a 4-digit PIN is auto-generated when omitted
    and returned once via StaffResponse.initial_pin.
    """
    staff_name: str = Field(max_length=255)
    pin: str | None = Field(default=None, min_length=4, max_length=4, pattern=r"^\d{4}$")
    role: str = Field(default="STAFF", pattern=r"^(STAFF|MANAGER)$")
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class StaffUpdate(BaseModel):
    staff_name: str | None = None
    is_active: bool | None = None


class PinRotation(BaseModel):
    # Exactly 4 digits — matches the staff till's PinKeypad, which is a
    # fixed 4-digit UI. A longer PIN can never be entered on the till.
    new_pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class StaffResponse(BaseModel):
    id: uuid.UUID
    staff_name: str | None
    email: str | None
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    # Populated only by POST /business/staff when a PIN was auto-generated —
    # shown once, never returned by GET/PUT.
    initial_pin: str | None = None
    model_config = {"from_attributes": True}


class PinRotateResponse(BaseModel):
    staff_id: uuid.UUID
    pin: str  # Plaintext PIN returned once on rotate only


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[StaffResponse])
async def list_staff(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """List all staff + manager users for this business."""
    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.business_id == user.business_id,
            BusinessUser.role.in_(["STAFF", "MANAGER"]),
        ).order_by(BusinessUser.staff_name, BusinessUser.email)
    )
    return [StaffResponse.model_validate(u) for u in result.scalars().all()]


@router.post("", response_model=StaffResponse, status_code=201)
async def create_staff(
    body: StaffCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new staff or manager user.

    STAFF role: pin is optional — a 4-digit PIN is auto-generated when
    omitted and returned once via the response's initial_pin field.
    Authenticates via POST /auth/pin.
    MANAGER role: requires email + password. Authenticates via POST /auth/login.
    """
    # Only OWNER can create MANAGER
    if body.role == "MANAGER" and user.role != "OWNER":
        raise AppError("INSUFFICIENT_ROLE", "Only the owner can create managers", 403)

    # Validate required fields based on role
    generated_pin: str | None = None
    if body.role == "MANAGER":
        if not body.email:
            raise AppError("MISSING_EMAIL", "Email is required for managers", 422)
        if not body.password:
            raise AppError("MISSING_PASSWORD", "Password is required for managers", 422)
    else:  # STAFF
        if not body.pin:
            generated_pin = _generate_pin()

    # Check email uniqueness if provided
    if body.email:
        email_lower = body.email.lower().strip()
        existing = await db.execute(
            select(BusinessUser.id).where(BusinessUser.email == email_lower)
        )
        if existing.scalar_one_or_none():
            raise DuplicateError("User", "email")

    pin_to_store = body.pin or generated_pin
    staff = BusinessUser(
        business_id=user.business_id,
        role=body.role,
        staff_name=body.staff_name,
        email=body.email.lower().strip() if body.email else None,
        password_hash=hash_password(body.password) if body.password else None,
        pin_hash=hash_pin(pin_to_store) if pin_to_store else None,
        pin_updated_at=datetime.now(timezone.utc) if pin_to_store else None,
        is_active=True,
    )
    db.add(staff)
    await db.commit()
    await db.refresh(staff)

    # Transient attribute — not a mapped column, never persisted.
    staff.initial_pin = generated_pin
    return StaffResponse.model_validate(staff)


@router.put("/{staff_id}", response_model=StaffResponse)
async def update_staff(
    staff_id: uuid.UUID,
    body: StaffUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Update staff name or active status."""
    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == user.business_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise NotFoundError("Staff", str(staff_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(staff, field, value)

    await db.commit()
    await db.refresh(staff)
    return StaffResponse.model_validate(staff)


@router.post("/{staff_id}/pin", status_code=200, response_model=PinRotateResponse)
async def rotate_pin(
    staff_id: uuid.UUID,
    body: PinRotation,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Rotate a staff member's PIN (caller provides new PIN)."""
    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == user.business_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise NotFoundError("Staff", str(staff_id))

    staff.pin_hash = hash_pin(body.new_pin)
    staff.pin_updated_at = datetime.now(timezone.utc)
    staff.failed_login_attempts = 0
    staff.locked_until = None
    await db.commit()

    return PinRotateResponse(staff_id=staff.id, pin=body.new_pin)


@router.post(
    "/{staff_id}/pin/rotate",
    response_model=PinRotateResponse,
    summary="Auto-generate a new PIN for staff",
)
async def auto_rotate_pin(
    staff_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-generate a new random 4-digit PIN, hash it, and return plaintext once.
    This is the recommended way to rotate PINs. 4 digits matches the staff
    till's fixed-length PinKeypad — anything longer can't be entered there.
    """
    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == user.business_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise NotFoundError("Staff", str(staff_id))

    new_pin = _generate_pin()

    staff.pin_hash = hash_pin(new_pin)
    staff.pin_updated_at = datetime.now(timezone.utc)
    staff.failed_login_attempts = 0
    staff.locked_until = None
    await db.commit()

    return PinRotateResponse(staff_id=staff.id, pin=new_pin)


@router.delete("/{staff_id}", status_code=204)
async def deactivate_staff(
    staff_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a staff user (soft delete)."""
    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == user.business_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise NotFoundError("Staff", str(staff_id))

    # Prevent deactivating yourself
    if staff.id == user.user_id:
        raise AppError("SELF_DEACTIVATION", "Cannot deactivate your own account", 422)

    staff.is_active = False

    # Kill their sessions immediately: revoke all live refresh tokens.
    # Their access token dies on next request via the rbac is_active check.
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == staff.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()


@router.delete("/{staff_id}/permanent", status_code=204)
async def delete_staff_permanently(
    staff_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently delete a staff/manager record — irreversible, unlike the
    soft-deactivate above. OWNER only, and only once the account is already
    inactive (deactivate first) — a two-step gate against deleting someone
    still on shift by mistake.

    Safe to hard-delete: refresh_tokens and push_subscriptions cascade on
    delete; order history (Order.cancelled_by_user_id,
    OrderEvent.changed_by_user_id) intentionally has no FK to business_users,
    so past orders simply keep the (now orphaned) id.
    """
    if user.role != "OWNER":
        raise AppError("INSUFFICIENT_ROLE", "Only the owner can permanently delete staff", 403)

    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == user.business_id,
        )
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise NotFoundError("Staff", str(staff_id))

    if staff.id == user.user_id:
        raise AppError("SELF_DELETION", "Cannot delete your own account", 422)

    if staff.is_active:
        raise AppError(
            "STILL_ACTIVE",
            "Deactivate this staff member before permanently deleting them",
            422,
        )

    await db.delete(staff)
    await db.commit()
