"""
Staff management routes — Owner/Manager can create, list, update, and deactivate staff users.
Includes PIN rotation.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.core.security import hash_pin
from backend.app.db.session import get_db
from shared.models.user import BusinessUser

router = APIRouter(prefix="/business/staff", tags=["staff"])


class StaffCreate(BaseModel):
    staff_name: str = Field(max_length=255)
    pin: str = Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")
    role: str = Field(default="STAFF", pattern=r"^(STAFF|MANAGER)$")
    email: str | None = None  # Optional for managers


class StaffUpdate(BaseModel):
    staff_name: str | None = None
    is_active: bool | None = None


class PinRotation(BaseModel):
    new_pin: str = Field(min_length=4, max_length=8, pattern=r"^\d{4,8}$")


class StaffResponse(BaseModel):
    id: uuid.UUID
    staff_name: str | None
    email: str | None
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=list[StaffResponse])
async def list_staff(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """List all staff users for this business."""
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
    """Create a new staff user with a PIN."""
    # Only OWNER can create MANAGER
    if body.role == "MANAGER" and user.role != "OWNER":
        raise AppError("INSUFFICIENT_ROLE", "Only the owner can create managers", 403)

    from backend.app.core.security import hash_password

    staff = BusinessUser(
        business_id=user.business_id,
        role=body.role,
        staff_name=body.staff_name,
        pin_hash=hash_pin(body.pin),
        pin_updated_at=datetime.now(timezone.utc),
        email=body.email.lower().strip() if body.email else None,
        password_hash=hash_password(body.pin + "Temp!") if body.email else None,  # Temp password for managers
    )
    db.add(staff)
    await db.commit()
    await db.refresh(staff)
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


@router.post("/{staff_id}/pin", status_code=204)
async def rotate_pin(
    staff_id: uuid.UUID,
    body: PinRotation,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Rotate a staff member's PIN."""
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
    await db.commit()
