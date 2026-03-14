"""
Admin auth routes — POST /admin/login, /admin/refresh, /admin/logout
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import (
    create_admin_access_token,
    hash_refresh_token,
    verify_password,
)
from admin_api.app.core.config import get_admin_settings
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
