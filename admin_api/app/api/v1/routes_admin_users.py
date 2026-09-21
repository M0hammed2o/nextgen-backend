"""
Admin-user management — SUPER_ADMIN only.

Before this, the only way to create an admin_users row was
`scripts/seed.py`, a local script with no self-service path in production.
This gives a SUPER_ADMIN an API to create ONBOARDER (or additional
SUPER_ADMIN) logins directly, which is what makes Milestone B's
sales-partner onboarding usable without shell/DB access — see
routes_businesses.py for what an ONBOARDER can then do with that login.

Known gap, stated rather than silently worked around: `AdminUser` has no
`must_change_password` column (unlike `BusinessUser`), so unlike the
owner/manager creation flow, a freshly created admin's temporary password
is NOT force-changed on first login. Adding that would need its own
migration and a change to POST /admin/login's flow — out of this branch's
"smallest safe path" scope. See this task's DOC NOTE for TODO.md.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.app.core.deps import require_super_admin
from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from shared.enums import AuditScope
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.user import AdminRefreshToken
from shared.utils import generate_temp_password

router = APIRouter(prefix="/admin/admin-users", tags=["admin-admin-users"])


class AdminUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    # Shown once, only by POST and .../reset-password.
    temporary_password: str | None = None
    model_config = {"from_attributes": True}


class AdminUserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(max_length=255)
    role: str = Field(pattern=r"^(SUPER_ADMIN|ONBOARDER)$")


class AdminUserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    role: str | None = Field(default=None, pattern=r"^(SUPER_ADMIN|ONBOARDER)$")
    is_active: bool | None = None


async def _active_super_admin_count(db: AsyncSession, excluding_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(AdminUser).where(
        AdminUser.role == AdminRole.SUPER_ADMIN, AdminUser.is_active.is_(True),
    )
    if excluding_id is not None:
        stmt = stmt.where(AdminUser.id != excluding_id)
    return int((await db.execute(stmt)).scalar() or 0)


@router.get("", response_model=list[AdminUserResponse])
async def list_admin_users(
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AdminUser).order_by(AdminUser.created_at.desc()))
    return [AdminUserResponse.model_validate(a) for a in result.scalars().all()]


@router.post("", response_model=AdminUserResponse, status_code=201)
async def create_admin_user(
    body: AdminUserCreate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    email_lower = body.email.lower().strip()
    existing = await db.execute(select(AdminUser.id).where(AdminUser.email == email_lower))
    if existing.scalar_one_or_none():
        raise DuplicateError("Admin user", "email")

    temp_password = generate_temp_password()
    admin = AdminUser(
        email=email_lower,
        password_hash=hash_password(temp_password),
        display_name=body.display_name,
        role=body.role,
        is_active=True,
    )
    db.add(admin)
    await db.flush()
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=None,
        actor_user_id=user.user_id,
        action="admin.admin_user.created",
        target_type="admin_user",
        target_id=admin.id,
        # Never log the temporary password itself — only metadata.
        diff_json={"email": email_lower, "role": body.role},
    ))
    await db.commit()
    await db.refresh(admin)

    admin.temporary_password = temp_password
    return AdminUserResponse.model_validate(admin)


@router.patch("/{admin_id}", response_model=AdminUserResponse)
async def update_admin_user(
    admin_id: uuid.UUID,
    body: AdminUserPatch,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(AdminUser, admin_id)
    if not target:
        raise NotFoundError("Admin user", str(admin_id))

    changes = body.model_dump(exclude_unset=True)

    is_self = target.id == user.user_id
    would_deactivate = changes.get("is_active") is False
    would_demote = "role" in changes and changes["role"] != AdminRole.SUPER_ADMIN and target.role == AdminRole.SUPER_ADMIN

    if is_self and (would_deactivate or would_demote):
        raise AppError(
            "CANNOT_MODIFY_SELF",
            "You cannot deactivate or demote your own admin account",
            403,
        )

    # Last-SUPER_ADMIN protection: block a deactivation or demotion that
    # would leave zero active SUPER_ADMINs platform-wide.
    if target.role == AdminRole.SUPER_ADMIN and target.is_active and (would_deactivate or would_demote):
        remaining = await _active_super_admin_count(db, excluding_id=target.id)
        if remaining == 0:
            raise AppError(
                "LAST_SUPER_ADMIN",
                "Cannot deactivate or demote the last active SUPER_ADMIN",
                409,
            )

    for field, value in changes.items():
        setattr(target, field, value)

    if would_deactivate:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(AdminRefreshToken)
            .where(AdminRefreshToken.user_id == target.id, AdminRefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )

    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=None,
        actor_user_id=user.user_id,
        action="admin.admin_user.updated",
        target_type="admin_user",
        target_id=target.id,
        diff_json=changes,
    ))
    await db.commit()
    await db.refresh(target)
    return AdminUserResponse.model_validate(target)


@router.post("/{admin_id}/reset-password", response_model=AdminUserResponse)
async def reset_admin_user_password(
    admin_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import update as sa_update

    target = await db.get(AdminUser, admin_id)
    if not target:
        raise NotFoundError("Admin user", str(admin_id))

    temp_password = generate_temp_password()
    target.password_hash = hash_password(temp_password)
    target.failed_login_attempts = 0
    target.locked_until = None

    await db.execute(
        sa_update(AdminRefreshToken)
        .where(AdminRefreshToken.user_id == target.id, AdminRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=None,
        actor_user_id=user.user_id,
        action="admin.admin_user.password_reset",
        target_type="admin_user",
        target_id=target.id,
        diff_json={"email": target.email},
    ))
    await db.commit()
    await db.refresh(target)

    target.temporary_password = temp_password
    return AdminUserResponse.model_validate(target)
