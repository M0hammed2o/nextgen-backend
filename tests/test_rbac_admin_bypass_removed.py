"""
F5 fix (Wave 0A verifier): backend.app.core.rbac.require_business_access used
to let ANY SUPER_ADMIN token through with business_id=None ("super admins can
pass through"). That let admin-plane tokens reach business-plane routes
without ever being re-validated against the DB the way admin_api's
get_current_admin does (22 routes returned 200 with empty data, 2 returned an
unhandled 500 — e.g. GET /loyalty/program). No frontend depended on this (the
admin suite only calls admin_api — verified by a repo-wide grep for
`/v1/business`).

These are dependency-level tests (no DB, no HTTP layer) — the fastest,
most direct way to prove the exact code path that changed. A full HTTP-level
sweep across every business-plane route (matching the independent verifier's
own adversarial script) is a heavier, DB-touching test; see
tests/integration/test_workflow_platform_administration.py and
tests/integration/test_workflow_commercial.py for the existing pattern this
repo uses for that kind of full-stack check.
"""
import uuid

import pytest

from backend.app.core.rbac import AuthUser, require_business_access, require_owner_or_manager, require_staff_or_above


def _admin_auth(role: str) -> AuthUser:
    # Admin-plane tokens always carry business_id=None (see get_current_user —
    # the "bid" claim is never set for an admin_access token).
    return AuthUser(user_id=uuid.uuid4(), business_id=None, role=role, token_type="admin_access")


def _business_auth(role: str, business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "ONBOARDER"])
async def test_admin_plane_token_rejected_by_bare_business_access(role):
    with pytest.raises(Exception) as exc:
        await require_business_access()(user=_admin_auth(role))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "ONBOARDER"])
async def test_admin_plane_token_rejected_by_owner_or_manager(role):
    """require_owner_or_manager backs /loyalty/program, /business/settings, etc."""
    with pytest.raises(Exception) as exc:
        await require_owner_or_manager(user=_admin_auth(role))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"


@pytest.mark.parametrize("role", ["SUPER_ADMIN", "ONBOARDER"])
async def test_admin_plane_token_rejected_by_staff_or_above(role):
    """require_staff_or_above backs order operations (routes_orders.py)."""
    with pytest.raises(Exception) as exc:
        await require_staff_or_above(user=_admin_auth(role))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"


async def test_super_admin_no_longer_bypasses_role_check_either():
    """
    Before the fix, SUPER_ADMIN returned immediately without even reaching
    the role check — confirm it's now rejected the same way as any other
    business_id=None caller, regardless of the allowed_roles list.
    """
    with pytest.raises(Exception) as exc:
        await require_business_access(["STAFF", "MANAGER", "OWNER"])(user=_admin_auth("SUPER_ADMIN"))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"


# ── Real business-plane tokens are completely unaffected ───────────────────

@pytest.mark.parametrize("role,dep", [
    ("OWNER", require_owner_or_manager),
    ("MANAGER", require_owner_or_manager),
    ("STAFF", require_staff_or_above),
])
async def test_real_business_tokens_still_pass(role, dep):
    user = _business_auth(role)
    result = await dep(user=user)
    assert result is user


async def test_business_token_with_wrong_role_still_gets_insufficient_role():
    """A real business user without the right role — unrelated to this fix,
    confirms INSUFFICIENT_ROLE (not NO_BUSINESS_CONTEXT) still fires correctly
    for a token that DOES have a business_id."""
    with pytest.raises(Exception) as exc:
        await require_owner_or_manager(user=_business_auth("STAFF"))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "INSUFFICIENT_ROLE"
