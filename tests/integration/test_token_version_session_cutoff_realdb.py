"""
Real-Postgres proof that changing a credential ends sessions IMMEDIATELY,
not when the access token expires (BUG-0206 follow-up, migration
0061_business_user_token_version).

Exercises the real chain: real tokens from AuthService, the real per-request
check in rbac.get_current_user against a real business_users row, and the
real routes that change credentials.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from backend.app.api.v1.routes_staff import auto_rotate_pin, update_staff, StaffUpdate
from backend.app.core.config import get_settings
from backend.app.core.rbac import AuthUser, get_current_user
from backend.app.core.security import hash_password, hash_pin
from backend.app.services.auth_service import AuthService
from shared.models.business import Business
from shared.models.user import BusinessUser, RefreshToken

pytestmark = pytest.mark.asyncio

OWNER_PW = "Owner-pass-1"
STAFF_PIN = "4711"


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def _authenticates(db, token: str) -> AuthUser:
    return await get_current_user(request=None, credentials=_creds(token), db=db)


async def _refused_with(db, token: str) -> str:
    with pytest.raises(HTTPException) as exc:
        await _authenticates(db, token)
    return exc.value.detail["code"]


async def _make_users(db, business: Business) -> tuple[BusinessUser, BusinessUser]:
    unique = uuid.uuid4().hex[:8]
    owner = BusinessUser(
        business_id=business.id, role="OWNER", email=f"owner-{unique}@example.com",
        password_hash=hash_password(OWNER_PW), is_active=True,
    )
    staff = BusinessUser(
        business_id=business.id, role="STAFF", staff_name=f"Till {unique}",
        pin_hash=hash_pin(STAFF_PIN), is_active=True,
    )
    db.add_all([owner, staff])
    await db.commit()
    await db.refresh(owner)
    await db.refresh(staff)
    return owner, staff


async def test_new_users_start_at_version_zero_and_tokens_carry_it(db, test_business):
    owner, _ = await _make_users(db, test_business)
    assert owner.token_version == 0  # server default from migration 0061
    _, _, access, _ = await AuthService(db).login_email(email=owner.email, password=OWNER_PW)
    claims = jwt.decode(access, options={"verify_signature": False})
    assert claims["tv"] == 0
    assert (await _authenticates(db, access)).user_id == owner.id


async def test_pin_rotation_kills_the_existing_staff_access_token_immediately(db, test_business):
    owner, staff = await _make_users(db, test_business)
    _, _, old_access, old_refresh = await AuthService(db).login_pin(
        business_code=test_business.business_code, staff_id=staff.id, pin=STAFF_PIN,
    )
    assert (await _authenticates(db, old_access)).user_id == staff.id

    owner_auth = AuthUser(user_id=owner.id, business_id=test_business.id, role="OWNER", token_type="access")
    rotated = await auto_rotate_pin(staff_id=staff.id, user=owner_auth, db=db)

    # The old access token is refused on its very next request, well before expiry.
    assert await _refused_with(db, old_access) == "SESSION_REVOKED"
    # Its refresh token is revoked too.
    row = (await db.execute(select(RefreshToken).where(RefreshToken.user_id == staff.id))).scalars().all()
    assert row and all(r.revoked_at is not None for r in row)

    # A fresh login with the NEW PIN works and carries the new version.
    _, _, new_access, _ = await AuthService(db).login_pin(
        business_code=test_business.business_code, staff_id=staff.id, pin=rotated.pin,
    )
    assert jwt.decode(new_access, options={"verify_signature": False})["tv"] == 1
    assert (await _authenticates(db, new_access)).user_id == staff.id


async def test_password_change_kills_other_sessions_but_keeps_the_new_one(db, test_business):
    owner, _ = await _make_users(db, test_business)
    svc = AuthService(db)
    _, _, other_session_access, _ = await svc.login_email(email=owner.email, password=OWNER_PW)
    assert (await _authenticates(db, other_session_access)).user_id == owner.id

    _, _, new_access, _ = await svc.set_password(
        email=owner.email, current_password=OWNER_PW, new_password="Brand-new-pass-2",
    )

    assert await _refused_with(db, other_session_access) == "SESSION_REVOKED"
    assert (await _authenticates(db, new_access)).user_id == owner.id


async def test_put_deactivation_then_reactivation_does_not_revive_old_tokens(db, test_business):
    owner, staff = await _make_users(db, test_business)
    _, _, old_access, _ = await AuthService(db).login_pin(
        business_code=test_business.business_code, staff_id=staff.id, pin=STAFF_PIN,
    )
    owner_auth = AuthUser(user_id=owner.id, business_id=test_business.id, role="OWNER", token_type="access")
    await update_staff(staff_id=staff.id, body=StaffUpdate(is_active=False), user=owner_auth, db=db)
    await update_staff(staff_id=staff.id, body=StaffUpdate(is_active=True), user=owner_auth, db=db)

    # Active again, but the pre-deactivation token is still dead.
    assert await _refused_with(db, old_access) == "SESSION_REVOKED"


async def test_pre_deploy_token_without_tv_claim_still_works_until_a_change(db, test_business):
    """Tokens minted before this deploy have no 'tv' claim: treated as 0, nobody is logged out by the deploy."""
    owner, staff = await _make_users(db, test_business)
    settings = get_settings()
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"sub": str(staff.id), "bid": str(test_business.id), "role": "STAFF",
         "iat": now, "exp": now + timedelta(minutes=10), "type": "access"},
        settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
    )
    assert (await _authenticates(db, legacy)).user_id == staff.id

    owner_auth = AuthUser(user_id=owner.id, business_id=test_business.id, role="OWNER", token_type="access")
    await auto_rotate_pin(staff_id=staff.id, user=owner_auth, db=db)
    assert await _refused_with(db, legacy) == "SESSION_REVOKED"


async def test_a_forged_tv_claim_cannot_resurrect_a_session(db, test_business):
    """tv is inside the signed JWT; a mismatched value (old or future) is refused."""
    owner, staff = await _make_users(db, test_business)
    owner_auth = AuthUser(user_id=owner.id, business_id=test_business.id, role="OWNER", token_type="access")
    await auto_rotate_pin(staff_id=staff.id, user=owner_auth, db=db)  # version -> 1
    settings = get_settings()
    now = datetime.now(timezone.utc)
    for tv in (0, 2, "x"):
        token = jwt.encode(
            {"sub": str(staff.id), "bid": str(test_business.id), "role": "STAFF",
             "iat": now, "exp": now + timedelta(minutes=10), "type": "access", "tv": tv},
            settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
        )
        assert await _refused_with(db, token) in {"SESSION_REVOKED", "INVALID_TOKEN"}
