"""
Auth service — handles login (email+password, business_code+PIN),
token creation, refresh, logout, and account lockout.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.security import (
    create_access_token,
    hash_refresh_token,
    verify_password,
    verify_pin,
)
from shared.models.business import Business
from shared.models.user import BusinessUser, RefreshToken
from shared.utils import generate_refresh_token

settings = get_settings()


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Email + Password Login (OWNER / MANAGER) ────────────────────────

    async def login_email(
        self, email: str, password: str, ip_address: str | None = None
    ) -> tuple[BusinessUser, Business, str, str]:
        """
        Authenticate via email + password.
        Returns (user, business, access_token, refresh_token_raw).
        Raises AppError on failure.
        """
        stmt = (
            select(BusinessUser)
            .where(BusinessUser.email == email.lower().strip())
        )
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

        # Check lockout
        self._check_lockout(user)

        if not user.password_hash or not verify_password(password, user.password_hash):
            await self._record_failed_login(user)
            raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

        if not user.is_active:
            raise AppError("ACCOUNT_DISABLED", "This account has been disabled", 403)

        # Load business
        business = await self._load_business(user.business_id)
        self._check_business_active(business)

        # Success — reset failed attempts & update last_login
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = datetime.now(timezone.utc)

        # Create tokens
        access_token = create_access_token(user.id, user.business_id, user.role)
        refresh_raw = generate_refresh_token()
        await self._store_refresh_token(user.id, refresh_raw, ip_address)

        await self.db.commit()
        return user, business, access_token, refresh_raw

    # ── Business Code + PIN Login (STAFF) ────────────────────────────────

    async def login_pin(
        self, business_code: str, pin: str, ip_address: str | None = None
    ) -> tuple[BusinessUser, Business, str, str]:
        """
        Authenticate staff via business_code + PIN.
        Returns (user, business, access_token, refresh_token_raw).
        """
        # Find business by code
        stmt = select(Business).where(Business.business_code == business_code.upper())
        result = await self.db.execute(stmt)
        business = result.scalar_one_or_none()

        if not business:
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        self._check_business_active(business)

        # Find matching staff user by trying each staff PIN
        stmt = (
            select(BusinessUser)
            .where(
                BusinessUser.business_id == business.id,
                BusinessUser.role == "STAFF",
                BusinessUser.is_active == True,
            )
        )
        result = await self.db.execute(stmt)
        staff_users = result.scalars().all()

        matched_user = None
        for staff in staff_users:
            self._check_lockout(staff)
            if staff.pin_hash and verify_pin(pin, staff.pin_hash):
                matched_user = staff
                break

        if not matched_user:
            # Increment failed attempts for all checked staff (prevents PIN enumeration)
            for staff in staff_users:
                if staff.pin_hash:
                    await self._record_failed_login(staff)
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        # Success
        matched_user.failed_login_attempts = 0
        matched_user.locked_until = None
        matched_user.last_login_at = datetime.now(timezone.utc)

        access_token = create_access_token(matched_user.id, business.id, matched_user.role)
        refresh_raw = generate_refresh_token()
        await self._store_refresh_token(matched_user.id, refresh_raw, ip_address)

        await self.db.commit()
        return matched_user, business, access_token, refresh_raw

    # ── Token Refresh (rotation) ─────────────────────────────────────────

    async def refresh_tokens(
        self, refresh_token_raw: str, ip_address: str | None = None
    ) -> tuple[str, str]:
        """
        Rotate refresh token: validate old, revoke old, issue new pair.
        Returns (new_access_token, new_refresh_token_raw).
        """
        token_hash = hash_refresh_token(refresh_token_raw)

        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
        )
        result = await self.db.execute(stmt)
        stored = result.scalar_one_or_none()

        if not stored:
            raise AppError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or revoked", 401)

        if stored.expires_at < datetime.now(timezone.utc):
            raise AppError("REFRESH_TOKEN_EXPIRED", "Refresh token has expired", 401)

        # Load user
        stmt = select(BusinessUser).where(BusinessUser.id == stored.user_id)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.is_active:
            raise AppError("ACCOUNT_DISABLED", "Account is disabled", 403)

        # Revoke old token
        stored.revoked_at = datetime.now(timezone.utc)

        # Issue new pair
        access_token = create_access_token(user.id, user.business_id, user.role)
        new_refresh_raw = generate_refresh_token()
        await self._store_refresh_token(user.id, new_refresh_raw, ip_address)

        await self.db.commit()
        return access_token, new_refresh_raw

    # ── Logout ───────────────────────────────────────────────────────────

    async def logout(self, refresh_token_raw: str) -> None:
        """Revoke a refresh token on logout."""
        token_hash = hash_refresh_token(refresh_token_raw)
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        )
        result = await self.db.execute(stmt)
        stored = result.scalar_one_or_none()
        if stored:
            stored.revoked_at = datetime.now(timezone.utc)
            await self.db.commit()

    # ── Get User for /me ─────────────────────────────────────────────────

    async def get_user_with_business(
        self, user_id: uuid.UUID
    ) -> tuple[BusinessUser, Business | None]:
        """Load user + their business for the /me endpoint."""
        stmt = select(BusinessUser).where(BusinessUser.id == user_id)
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()
        if not user:
            raise NotFoundError("User", str(user_id))

        business = None
        if user.business_id:
            business = await self._load_business(user.business_id)

        return user, business

    # ── Private Helpers ──────────────────────────────────────────────────

    async def _load_business(self, business_id: uuid.UUID) -> Business:
        stmt = select(Business).where(Business.id == business_id)
        result = await self.db.execute(stmt)
        business = result.scalar_one_or_none()
        if not business:
            raise NotFoundError("Business", str(business_id))
        return business

    def _check_business_active(self, business: Business) -> None:
        if not business.is_active:
            from backend.app.core.errors import BusinessSuspendedError
            raise BusinessSuspendedError(business.suspended_reason)

    def _check_lockout(self, user: BusinessUser) -> None:
        if user.locked_until and user.locked_until > datetime.now(timezone.utc):
            raise AppError(
                "ACCOUNT_LOCKED",
                f"Account locked. Try again after {user.locked_until.isoformat()}",
                status_code=423,
            )

    async def _record_failed_login(self, user: BusinessUser) -> None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.ACCOUNT_LOCKOUT_ATTEMPTS:
            user.locked_until = datetime.now(timezone.utc) + timedelta(
                minutes=settings.ACCOUNT_LOCKOUT_MINUTES
            )
        await self.db.flush()

    async def _store_refresh_token(
        self, user_id: uuid.UUID, raw_token: str, ip_address: str | None
    ) -> None:
        rt = RefreshToken(
            user_id=user_id,
            token_hash=hash_refresh_token(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            ip_address=ip_address,
        )
        self.db.add(rt)
        await self.db.flush()
