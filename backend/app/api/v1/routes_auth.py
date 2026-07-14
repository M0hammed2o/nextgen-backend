"""
Auth routes — POST /auth/login, /auth/pin, /auth/refresh, /auth/logout, GET /me
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.ratelimit import limiter
from backend.app.core.rbac import AuthUser, get_current_user
from backend.app.db.session import get_db
from backend.app.schemas.auth import (
    AuthUserInfo,
    EmailLoginRequest,
    LogoutRequest,
    MeResponse,
    PinLoginRequest,
    RefreshRequest,
    SetPasswordRequest,
    StaffDirectoryEntry,
    StaffDirectoryRequest,
    StaffDirectoryResponse,
    TokenPair,
    TokenResponse,
)
from backend.app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login", response_model=TokenResponse)
@limiter.limit(f"{settings.LOGIN_RATE_LIMIT_PER_MINUTE}/minute")
async def login_email(
    body: EmailLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with email + password (OWNER / MANAGER)."""
    service = AuthService(db)
    user, business, access_token, refresh_raw = await service.login_email(
        email=body.email,
        password=body.password,
        ip_address=_client_ip(request),
    )
    return TokenResponse(
        tokens=TokenPair(
            access_token=access_token,
            refresh_token=refresh_raw,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
        user=AuthUserInfo(
            id=user.id,
            email=user.email,
            staff_name=user.staff_name,
            role=user.role,
            business_id=business.id,
            business_name=business.name,
        ),
    )


@router.post("/set-password", response_model=TokenResponse)
@limiter.limit(f"{settings.LOGIN_RATE_LIMIT_PER_MINUTE}/minute")
async def set_password(
    body: SetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Complete a forced password reset (OWNER/MANAGER). Re-verifies the
    current (temporary) password, sets a new one, and auto-logs-in — same
    response shape as /auth/login so the frontend lands straight in the
    dashboard.
    """
    service = AuthService(db)
    user, business, access_token, refresh_raw = await service.set_password(
        email=body.email,
        current_password=body.current_password,
        new_password=body.new_password,
        ip_address=_client_ip(request),
    )
    return TokenResponse(
        tokens=TokenPair(
            access_token=access_token,
            refresh_token=refresh_raw,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
        user=AuthUserInfo(
            id=user.id,
            email=user.email,
            staff_name=user.staff_name,
            role=user.role,
            business_id=business.id,
            business_name=business.name,
        ),
    )


@router.post("/pin/staff", response_model=StaffDirectoryResponse)
@limiter.limit(f"{settings.PIN_RATE_LIMIT_PER_MINUTE}/minute")
async def staff_directory(
    body: StaffDirectoryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Pick-your-name step of staff login: returns active, PIN-enabled staff
    (id + name only) for a business code. Unauthenticated by design —
    rate-limited, and an unknown code gets the same 401 as a wrong PIN.
    """
    service = AuthService(db)
    business, staff = await service.get_staff_directory(body.business_code)
    return StaffDirectoryResponse(
        business_name=business.name,
        staff=[
            StaffDirectoryEntry(id=s.id, staff_name=s.staff_name or "Staff")
            for s in staff
        ],
    )


@router.post("/pin", response_model=TokenResponse)
@limiter.limit(f"{settings.PIN_RATE_LIMIT_PER_MINUTE}/minute")
async def login_pin(
    body: PinLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate a selected staff member with business_code + staff_id + PIN."""
    service = AuthService(db)
    user, business, access_token, refresh_raw = await service.login_pin(
        business_code=body.business_code,
        staff_id=body.staff_id,
        pin=body.pin,
        ip_address=_client_ip(request),
    )
    return TokenResponse(
        tokens=TokenPair(
            access_token=access_token,
            refresh_token=refresh_raw,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        ),
        user=AuthUserInfo(
            id=user.id,
            email=user.email,
            staff_name=user.staff_name,
            role=user.role,
            business_id=business.id,
            business_name=business.name,
        ),
    )


@router.post("/refresh", response_model=TokenPair)
@limiter.limit(f"{settings.REFRESH_RATE_LIMIT_PER_MINUTE}/minute")
async def refresh_token(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Rotate refresh token and get new access + refresh tokens."""
    service = AuthService(db)
    access_token, new_refresh = await service.refresh_tokens(
        refresh_token_raw=body.refresh_token,
        ip_address=_client_ip(request),
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=204)
async def logout(
    body: LogoutRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke refresh token on logout."""
    service = AuthService(db)
    await service.logout(body.refresh_token)


@router.get("/me", response_model=MeResponse)
async def get_me(
    user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current authenticated user's profile."""
    service = AuthService(db)
    db_user, business = await service.get_user_with_business(user.user_id)
    return MeResponse(
        id=db_user.id,
        email=db_user.email,
        staff_name=db_user.staff_name,
        role=db_user.role,
        business_id=db_user.business_id,
        business_name=business.name if business else None,
        last_login_at=db_user.last_login_at,
    )
