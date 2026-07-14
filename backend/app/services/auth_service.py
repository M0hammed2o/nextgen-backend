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
    hash_password,
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
            # Commit explicitly — get_db never commits, so without this the
            # failed-attempt counter is lost when the 401 propagates.
            await self.db.commit()
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

        if user.must_change_password:
            # They proved they know the (temporary) password — bookkeeping
            # above stands — but no tokens until they set a real one via
            # POST /auth/set-password.
            await self.db.commit()
            raise AppError(
                "PASSWORD_CHANGE_REQUIRED",
                "You must set a new password before continuing",
                403,
            )

        access_token, refresh_raw = await self._issue_tokens(user, ip_address)
        await self.db.commit()
        return user, business, access_token, refresh_raw

    # ── Forced Password Reset (OWNER / MANAGER) ──────────────────────────

    async def set_password(
        self, email: str, current_password: str, new_password: str, ip_address: str | None = None
    ) -> tuple[BusinessUser, Business, str, str]:
        """
        Complete a forced password reset: re-verify the current (temporary)
        password, set a real one, clear must_change_password, and issue
        tokens (auto-login). Returns (user, business, access_token, refresh_token_raw).
        """
        stmt = select(BusinessUser).where(BusinessUser.email == email.lower().strip())
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

        self._check_lockout(user)

        if not user.password_hash or not verify_password(current_password, user.password_hash):
            await self._record_failed_login(user)
            await self.db.commit()
            raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

        if not user.is_active:
            raise AppError("ACCOUNT_DISABLED", "This account has been disabled", 403)

        if new_password == current_password:
            raise AppError(
                "SAME_PASSWORD",
                "New password must be different from the current password",
                422,
            )

        business = await self._load_business(user.business_id)
        self._check_business_active(business)

        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = datetime.now(timezone.utc)

        access_token, refresh_raw = await self._issue_tokens(user, ip_address)
        await self.db.commit()
        return user, business, access_token, refresh_raw

    # ── Business Code + Staff + PIN Login (STAFF) ────────────────────────

    async def get_staff_directory(
        self, business_code: str
    ) -> tuple[Business, list[BusinessUser]]:
        """
        Return the active, PIN-enabled staff for the login screen's
        pick-your-name step. Raises the same generic 401 as login for an
        unknown code so business codes cannot be probed apart from PINs.
        """
        stmt = select(Business).where(Business.business_code == business_code.upper())
        result = await self.db.execute(stmt)
        business = result.scalar_one_or_none()

        if not business:
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        self._check_business_active(business)

        stmt = (
            select(BusinessUser)
            .where(
                BusinessUser.business_id == business.id,
                BusinessUser.role == "STAFF",
                BusinessUser.is_active == True,
                BusinessUser.pin_hash.is_not(None),
            )
            .order_by(BusinessUser.staff_name)
        )
        result = await self.db.execute(stmt)
        return business, list(result.scalars().all())

    async def login_pin(
        self,
        business_code: str,
        staff_id: uuid.UUID,
        pin: str,
        ip_address: str | None = None,
    ) -> tuple[BusinessUser, Business, str, str]:
        """
        Authenticate one specific staff member via business_code + staff_id + PIN.

        The staff member picks their name on the login screen, so exactly one
        bcrypt hash is verified and lockout applies to that user only — a
        wrong PIN can never lock out the rest of the till staff, and two
        staff sharing a PIN can never be misattributed.
        """
        stmt = select(Business).where(Business.business_code == business_code.upper())
        result = await self.db.execute(stmt)
        business = result.scalar_one_or_none()

        if not business:
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        self._check_business_active(business)

        stmt = select(BusinessUser).where(
            BusinessUser.id == staff_id,
            BusinessUser.business_id == business.id,
            BusinessUser.role == "STAFF",
            BusinessUser.is_active == True,
        )
        result = await self.db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.pin_hash:
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        self._check_lockout(user)

        if not verify_pin(pin, user.pin_hash):
            await self._record_failed_login(user)
            # Commit explicitly — get_db never commits, so without this the
            # failed-attempt counter is lost when the 401 propagates.
            await self.db.commit()
            raise AppError("INVALID_CREDENTIALS", "Invalid business code or PIN", 401)

        # Success
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = datetime.now(timezone.utc)

        access_token = create_access_token(
            user.id,
            business.id,
            user.role,
            expires_minutes=settings.STAFF_ACCESS_TOKEN_EXPIRE_MINUTES,
        )
        refresh_raw = generate_refresh_token()
        await self._store_refresh_token(user.id, refresh_raw, ip_address)

        await self.db.commit()
        return user, business, access_token, refresh_raw

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

        # Issue new pair (STAFF keeps the short-lived access token on refresh)
        access_token = create_access_token(
            user.id,
            user.business_id,
            user.role,
            expires_minutes=(
                settings.STAFF_ACCESS_TOKEN_EXPIRE_MINUTES if user.role == "STAFF" else None
            ),
        )
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

    async def _issue_tokens(
        self, user: BusinessUser, ip_address: str | None
    ) -> tuple[str, str]:
        """Create an access + refresh token pair for an OWNER/MANAGER user."""
        access_token = create_access_token(user.id, user.business_id, user.role)
        refresh_raw = generate_refresh_token()
        await self._store_refresh_token(user.id, refresh_raw, ip_address)
        return access_token, refresh_raw

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
