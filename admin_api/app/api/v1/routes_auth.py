"""
Admin auth routes — POST /admin/login, /admin/refresh, /admin/logout, GET /admin/me
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.ratelimit import limiter
from backend.app.core.rbac import AuthUser
from backend.app.core.security import (
    create_admin_access_token,
    hash_refresh_token,
    verify_password,
)
from admin_api.app.core.config import get_admin_settings
from admin_api.app.core.deps import get_current_admin
from shared.models.admin import AdminUser
from shared.models.user import AdminRefreshToken
from shared.utils import generate_refresh_token

router = APIRouter(prefix="/admin", tags=["admin-auth"])
settings = get_admin_settings()


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class AdminTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


# Reuse the DB session from backend (same database)
from backend.app.db.session import get_db


@router.post("/login", response_model=AdminTokenResponse)
@limiter.limit("5/minute")
async def admin_login(
    body: AdminLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Super Admin login with email + password."""
    result = await db.execute(
        select(AdminUser).where(AdminUser.email == body.email.lower().strip())
    )
    admin = result.scalar_one_or_none()

    if not admin:
        from backend.app.core.errors import AppError
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    # Check lockout
    if admin.locked_until and admin.locked_until > datetime.now(timezone.utc):
        from backend.app.core.errors import AppError
        raise AppError("ACCOUNT_LOCKED", "Account is locked", 423)

    if not verify_password(body.password, admin.password_hash):
        admin.failed_login_attempts += 1
        if admin.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_ATTEMPTS:
            admin.locked_until = datetime.now(timezone.utc) + timedelta(
                minutes=settings.ACCOUNT_LOCKOUT_MINUTES
            )
        await db.commit()
        from backend.app.core.errors import AppError
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    if not admin.is_active:
        from backend.app.core.errors import AppError
        raise AppError("ACCOUNT_DISABLED", "Admin account is disabled", 403)

    # Success
    admin.failed_login_attempts = 0
    admin.locked_until = None
    admin.last_login_at = datetime.now(timezone.utc)

    access_token = create_admin_access_token(admin.id, admin.role)
    refresh_raw = generate_refresh_token()

    rt = AdminRefreshToken(
        user_id=admin.id,
        token_hash=hash_refresh_token(refresh_raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        ip_address=request.client.host if request.client else None,
    )
    db.add(rt)
    await db.commit()

    return AdminTokenResponse(
        access_token=access_token,
        refresh_token=refresh_raw,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


class AdminRefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=AdminTokenResponse)
@limiter.limit("5/minute")
async def admin_refresh(
    body: AdminRefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Rotate an admin refresh token and issue a new access + refresh pair.

    Rate-limited the same as /admin/login (5/minute) — this endpoint had no
    limit at all before, which is a meaningful gap given a stolen/guessed
    refresh token could otherwise be hammered to mint fresh access tokens
    without ever touching the password-checking /login path.
    """
    from datetime import datetime, timezone
    from backend.app.core.security import hash_refresh_token, create_admin_access_token
    from shared.utils import generate_refresh_token
    from sqlalchemy import select

    token_hash = hash_refresh_token(body.refresh_token)
    result = await db.execute(
        select(AdminRefreshToken).where(AdminRefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if not stored or stored.revoked_at or stored.expires_at < now:
        from backend.app.core.errors import AppError
        raise AppError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired", 401)

    # Revoke old token
    stored.revoked_at = now

    # Load admin user for role
    from shared.models.admin import AdminUser
    admin_result = await db.execute(
        select(AdminUser).where(AdminUser.id == stored.user_id)
    )
    admin = admin_result.scalar_one_or_none()
    if not admin or not admin.is_active:
        from backend.app.core.errors import AppError
        raise AppError("ACCOUNT_DISABLED", "Admin account is disabled", 403)

    # Issue new pair
    new_access = create_admin_access_token(admin.id, admin.role)
    new_refresh_raw = generate_refresh_token()

    new_rt = AdminRefreshToken(
        user_id=admin.id,
        token_hash=hash_refresh_token(new_refresh_raw),
        expires_at=now + __import__("datetime").timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        ip_address=request.client.host if request.client else None,
    )
    db.add(new_rt)
    await db.commit()

    return AdminTokenResponse(
        access_token=new_access,
        refresh_token=new_refresh_raw,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


class AdminMeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool


@router.get("/me", response_model=AdminMeResponse)
async def admin_me(
    admin: AuthUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Who am I, and what can I do — the admin UI needs `role` to decide
    whether to show ONBOARDER's reduced navigation or SUPER_ADMIN's full
    one. Goes through get_current_admin (not just the token payload) so a
    demoted/deactivated admin sees that reflected immediately, same as
    every other admin_api route.
    """
    result = await db.execute(select(AdminUser).where(AdminUser.id == admin.user_id))
    row = result.scalar_one_or_none()
    if not row:
        from backend.app.core.errors import NotFoundError
        raise NotFoundError("Admin user", str(admin.user_id))
    return AdminMeResponse(
        id=row.id, email=row.email, display_name=row.display_name,
        role=row.role, is_active=row.is_active,
    )
