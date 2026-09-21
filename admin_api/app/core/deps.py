"""
Admin-plane auth dependencies with per-request DB re-validation, plus the
single least-privilege helper every ONBOARDER-accessible route must use.

WHY THIS FILE EXISTS
---------------------
`backend.app.core.rbac.get_current_user` decodes and validates JWT
signature/expiry for both planes, but only re-checks the database for
business-plane ("access") tokens (see its docstring/comment on the instant
staff-deactivation cutoff). An admin-plane ("admin_access") token's
`is_active` flag and `role` are trusted from the token's own claims alone,
for the token's full lifetime (`ACCESS_TOKEN_EXPIRE_MINUTES`) — confirmed
by reading `get_current_user`: the DB re-check block is gated on
`user.token_type == "access"` and never runs for `"admin_access"` tokens.

That gap is unacceptable for the new ONBOARDER role, which the design
explicitly requires to lose access "immediately" on deactivation or
demotion — and it was already a latent gap for SUPER_ADMIN too (a
deactivated admin keeps working access until their access token expires).

`get_current_admin` below closes it for every route in `admin_api` that
uses it: it loads the AdminUser row by id on every single request and
returns an `AuthUser` built from the CURRENT `is_active`/`role` columns,
never from the token's role claim. `require_super_admin` and
`require_onboarding_role` are built on top of it, so simply importing them
from here instead of `backend.app.core.rbac` is what gives a route
per-request re-validation — no other code at the call site needs to
change, since the return type is still `AuthUser`.
"""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, get_current_user
from backend.app.db.session import get_db
from shared.models.admin import AdminRole
from shared.models.admin import AdminUser
from shared.models.business import Business


async def get_current_admin(
    user: AuthUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuthUser:
    """
    Re-validate an admin-plane token against the live AdminUser row.

    Rejects business-plane tokens outright (this dependency is admin-only),
    and — the actual fix — rejects a token whose admin account has since
    been deactivated, and substitutes the DB's CURRENT role for whatever
    role the token itself claims, so a demotion takes effect on this very
    request rather than at the token's natural expiry or next login.
    """
    if user.token_type != "admin_access":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ADMIN_TOKEN_REQUIRED", "message": "Admin-plane token required"},
        )

    admin = await db.get(AdminUser, user.user_id)
    if not admin or not admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_DISABLED", "message": "Admin account is disabled"},
        )

    return AuthUser(
        user_id=user.user_id,
        business_id=None,
        role=admin.role,
        token_type=user.token_type,
    )


def require_admin_role(*allowed_roles: str):
    """Dependency factory — same shape as backend.app.core.rbac.require_role,
    but layered on the DB-revalidated get_current_admin above."""
    async def _check(admin: AuthUser = Depends(get_current_admin)) -> AuthUser:
        if admin.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "INSUFFICIENT_ROLE",
                    "message": f"Required role: {', '.join(allowed_roles)}. Your role: {admin.role}",
                },
            )
        return admin
    return _check


# Default-deny: everything platform-wide stays SUPER_ADMIN-only unless a
# route explicitly opts into require_onboarding_role instead.
require_super_admin = require_admin_role(AdminRole.SUPER_ADMIN)

# The onboarding allow-list only — see admin_api/app/api/v1/routes_businesses.py.
require_onboarding_role = require_admin_role(AdminRole.SUPER_ADMIN, AdminRole.ONBOARDER)


async def get_business_scoped(business_id: uuid.UUID, admin: AuthUser, db: AsyncSession) -> Business:
    """
    THE single least-privilege helper for every ONBOARDER-accessible
    business route. Every one of them must call this instead of
    `db.get(Business, business_id)` directly.

    SUPER_ADMIN: unrestricted, unchanged (platform-wide by design — see
    SECURITY.md's Tenant Isolation note on the admin plane).

    ONBOARDER: only a business they personally created
    (`onboarded_by_admin_id == admin.user_id`) AND whose onboarding is not
    yet marked complete (`onboarding_completed_at is None`). Anything else
    — someone else's business, a SUPER_ADMIN-created business, or their own
    business after handover — returns 404, not 403: a non-revealing
    response so an ONBOARDER token can't even learn that a given business
    ID exists once it's out of their reach.
    """
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    if admin.role == AdminRole.ONBOARDER:
        if biz.onboarded_by_admin_id != admin.user_id or biz.onboarding_completed_at is not None:
            raise NotFoundError("Business", str(business_id))

    return biz


async def get_business_commercial_scoped(business_id: uuid.UUID, admin: AuthUser, db: AsyncSession) -> Business:
    """
    The COMMERCIAL counterpart of `get_business_scoped` above — deliberately
    a second, separate helper rather than a parameter bolted onto the first,
    so the two scopes can never be silently merged or weakened into each
    other (see this task's design note and DECISIONS.md DOC NOTE).

    Used by every trial/invoice/commercial-status/dashboard route. SUPER_ADMIN:
    unrestricted, same as get_business_scoped. ONBOARDER: only a business they
    personally created (`onboarded_by_admin_id == admin.user_id`) — but,
    UNLIKE get_business_scoped, access does NOT end at
    `onboarding_completed_at`. A sales partner must keep starting/extending
    trials, issuing invoices and recording payments for their own clients
    after go-live/handover; only the CONFIGURATION routes gated by
    get_business_scoped lose access at that point. Same non-revealing 404
    (never 403) for out-of-scope access.
    """
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    if admin.role == AdminRole.ONBOARDER:
        if biz.onboarded_by_admin_id != admin.user_id:
            raise NotFoundError("Business", str(business_id))

    return biz
