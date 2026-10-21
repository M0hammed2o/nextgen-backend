"""
RBAC Dependencies — reusable FastAPI dependencies for authentication
and role-based access control.

Usage in routes:
    @router.get("/business/settings")
    async def get_settings(user: AuthUser = Depends(require_role(["OWNER", "MANAGER"]))):
        ...

Every business-facing endpoint enforces business_id from the JWT.
Never accept business_id from the client unless caller is SUPER_ADMIN.
"""

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.security import decode_access_token, decode_admin_token
from backend.app.db.session import get_db
from shared.models.user import BusinessUser

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class AuthUser:
    """Authenticated user context extracted from JWT."""
    user_id: uuid.UUID
    business_id: uuid.UUID | None
    role: str
    token_type: str  # "access" or "admin_access"


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AuthUser:
    """
    Extract and validate JWT from Authorization header.
    Returns AuthUser with user_id, business_id, and role.

    Business-plane tokens are tried first. Admin-plane tokens (signed with
    JWT_ADMIN_SECRET_KEY) are accepted as a fallback, but only for
    SUPER_ADMIN — the two planes are otherwise cryptographically separate.

    Business users are also checked against their live is_active flag so a
    deactivated staff member loses access on their next request, not when
    their token expires.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "Authorization header required"},
        )

    _expired = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "TOKEN_EXPIRED", "message": "Access token has expired"},
    )
    _invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "INVALID_TOKEN", "message": "Invalid access token"},
    )

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise _expired
    except jwt.InvalidTokenError:
        # Not a business-plane token — try the admin plane.
        try:
            payload = decode_admin_token(credentials.credentials)
        except jwt.ExpiredSignatureError:
            raise _expired
        except jwt.InvalidTokenError:
            raise _invalid
        # Milestone B (sales-partner onboarding): this function is the ONE
        # place every admin_api request authenticates through — including
        # ONBOARDER, a second admin-plane role added alongside SUPER_ADMIN
        # (shared/models/admin.py::AdminRole). Before this, this check
        # hardcoded "SUPER_ADMIN", which silently made every ONBOARDER
        # token invalid everywhere, including on admin_api's own routes —
        # found via a real-JWT integration test, not by inspection alone.
        # This does NOT grant ONBOARDER any new reach: business-plane
        # dependencies (require_business_access below) only special-case
        # role == "SUPER_ADMIN" for their cross-plane bypass, so an
        # ONBOARDER token — which also carries business_id=None — still
        # fails every business-plane role/tenant check exactly like any
        # other unrecognised role would. Per-request re-validation of the
        # CURRENT role/is_active (not just this token claim) happens one
        # layer up, in admin_api/app/core/deps.py::get_current_admin.
        from shared.models.admin import ADMIN_ROLES
        if payload.get("role") not in ADMIN_ROLES:
            raise _invalid

    bid_str = payload.get("bid")
    user = AuthUser(
        user_id=uuid.UUID(payload["sub"]),
        business_id=uuid.UUID(bid_str) if bid_str else None,
        role=payload["role"],
        token_type=payload.get("type", "access"),
    )

    # Instant cutoff for deactivated business users AND suspended businesses —
    # one indexed join, no extra round-trip. Business suspension was
    # previously only checked at login (auth_service.py's _check_business_active)
    # — a business suspended mid-session (e.g. subscription lapses) could
    # keep using an already-issued access token until it naturally expired.
    # This closes that gap for every request, not just login, matching the
    # per-user "instant cutoff" this comment already described.
    if user.token_type == "access":
        from shared.models.business import Business
        result = await db.execute(
            select(BusinessUser.is_active, Business.is_active, Business.suspended_reason)
            .join(Business, Business.id == BusinessUser.business_id)
            .where(BusinessUser.id == user.user_id)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "ACCOUNT_DISABLED", "message": "This account has been disabled"},
            )
        user_active, business_active, suspended_reason = row
        if not user_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "ACCOUNT_DISABLED", "message": "This account has been disabled"},
            )
        if not business_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "BUSINESS_SUSPENDED",
                    "message": "This business account has been suspended.",
                    "details": {"reason": suspended_reason} if suspended_reason else None,
                },
            )

    return user


def require_role(allowed_roles: list[str]):
    """
    Dependency factory: ensures the current user has one of the allowed roles.
    
    Usage:
        @router.get("/admin/businesses")
        async def list_businesses(user: AuthUser = Depends(require_role(["SUPER_ADMIN"]))):
            ...
    """
    async def _check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INSUFFICIENT_ROLE",
                    "message": f"Required role: {', '.join(allowed_roles)}. Your role: {user.role}",
                },
            )
        return user
    return _check


def require_business_access(allowed_roles: list[str] | None = None):
    """
    Dependency factory: ensures the user is authenticated AND has a business_id.
    Optionally filters by role.

    This is the standard dependency for all business-facing endpoints.
    business_id comes from the JWT, never from the client.

    Admin-plane tokens (SUPER_ADMIN, ONBOARDER — any role decoded via the
    admin-plane JWT secret, see get_current_user) are NEVER allowed through
    here, even for SUPER_ADMIN. There used to be a SUPER_ADMIN bypass so an
    admin token could "pass through" business-plane routes; it was removed
    (Wave 0A verifier finding F5) because it let admin tokens reach
    business-plane routes with business_id=None and was never re-validated
    against the DB for admin tokens the way admin_api's get_current_admin
    does. No frontend depends on this — the admin suite only calls
    admin_api, never /v1/business (verified by repo-wide grep). Admin
    tooling that genuinely needs to act on a specific business must go
    through admin_api's own routes, which are business_id-scoped from a
    path/query parameter under require_super_admin, not through this
    business-plane bypass.
    """
    async def _check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if not user.business_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "NO_BUSINESS_CONTEXT",
                    "message": "This endpoint requires a business-scoped user",
                },
            )

        if allowed_roles and user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INSUFFICIENT_ROLE",
                    "message": f"Required role: {', '.join(allowed_roles)}",
                },
            )

        return user
    return _check


# ── Convenience shortcuts ────────────────────────────────────────────────────

# Any authenticated business user
require_any_business_user = require_business_access()

# Owner or Manager only
require_owner_or_manager = require_business_access(["OWNER", "MANAGER"])

# Owner only
require_owner = require_business_access(["OWNER"])

# Staff, Manager, or Owner (for order operations)
require_staff_or_above = require_business_access(["STAFF", "MANAGER", "OWNER"])

# Super Admin only (for admin_api)
require_super_admin = require_role(["SUPER_ADMIN"])


async def require_pos_enabled(
    user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    POS/till entitlement gate (migration 0057_pos_entitlement).

    Deliberately the ONE shared home for this check, unlike the existing
    copy-pasted-per-file `_require_X_enabled(db, business_id)` pattern
    (`routes_inventory.py`, `routes_loyalty.py`, `routes_recipes.py`,
    `routes_stocktake.py`, `routes_wastage.py`, `routes_whatsapp_connect.py`
    all independently re-implement the same shape) — a prior audit flagged
    that pattern and asked that this new flag not repeat it. Used as
    `dependencies=[Depends(require_pos_enabled)]` on every route in
    `routes_pos.py` except `GET /business/pos/settings` (the Staff PWA
    reads that one as general tenant config, not just POS config).

    Fail-closed: re-reads `Business.pos_enabled` fresh from the DB on
    every single call — never trusts a cached/stale value (the JWT itself
    carries no entitlement claims at all, by design). A business row that
    can't be found is treated exactly like POS being off, never as
    "allowed by default".
    """
    if not user.business_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "NO_BUSINESS_CONTEXT",
                "message": "This endpoint requires a business-scoped user",
            },
        )

    from shared.models.business import Business
    result = await db.execute(
        select(Business.pos_enabled).where(Business.id == user.business_id)
    )
    pos_enabled = result.scalar_one_or_none()
    if not pos_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "POS_NOT_ENABLED",
                "message": "POS is not enabled for this business",
            },
        )
