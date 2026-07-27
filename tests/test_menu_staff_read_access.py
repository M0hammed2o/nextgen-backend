"""
Menu read-access RBAC change (2026-07-21): STAFF can now read the menu
catalog (GET /categories, /items, /add-ons) — needed to build a POS cart
(routes_pos.py). Every mutating menu route (POST/PUT/DELETE) stays
owner/manager-only, unchanged.

No dedicated RBAC-dependency test existed anywhere in this suite before —
this fills a real, pre-existing gap for the exact boundary being changed.
"""
import uuid

import pytest
from fastapi import HTTPException

from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above


def make_user(role: str, business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


@pytest.mark.parametrize("role", ["STAFF", "MANAGER", "OWNER"])
async def test_all_business_roles_pass_require_staff_or_above(role):
    user = make_user(role)
    result = await require_staff_or_above(user=user)
    assert result is user


async def test_staff_is_blocked_by_require_owner_or_manager():
    """STAFF still cannot create/edit/delete menu content — read-only widening only."""
    user = make_user("STAFF")
    with pytest.raises(HTTPException) as exc:
        await require_owner_or_manager(user=user)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["MANAGER", "OWNER"])
async def test_manager_and_owner_still_pass_require_owner_or_manager(role):
    user = make_user(role)
    result = await require_owner_or_manager(user=user)
    assert result is user
