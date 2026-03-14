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

from backend.app.core.security import decode_access_token

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
) -> AuthUser:
    """
    Extract and validate JWT from Authorization header.
    Returns AuthUser with user_id, business_id, and role.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "Authorization header required"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED", "message": "Access token has expired"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid access token"},
        )

    bid_str = payload.get("bid")
    return AuthUser(
        user_id=uuid.UUID(payload["sub"]),
        business_id=uuid.UUID(bid_str) if bid_str else None,
        role=payload["role"],
        token_type=payload.get("type", "access"),
    )


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
    """
    async def _check(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        # Super admins can pass through (they may use query params for business_id)
        if user.role == "SUPER_ADMIN":
            return user

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
