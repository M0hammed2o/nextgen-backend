"""
get_current_user() business-suspension cutoff (2026-07-24) — a business
suspended mid-session must lose API access on its very next request, not
just at their next login. Previously only checked at login
(auth_service.py::_check_business_active); the per-request dependency only
checked BusinessUser.is_active, never Business.is_active.

No test existed for get_current_user() at all before this — the JWT
decode step is mocked out so these tests isolate the DB-driven cutoff
behavior, which is the part that changed.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.core.rbac import get_current_user


class FakeCredentials:
    def __init__(self, token="fake-token"):
        self.credentials = token


class FakeResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class FakeDB:
    def __init__(self, row):
        self._row = row

    async def execute(self, stmt):
        return FakeResult(self._row)


def _payload(business_id):
    return {"sub": str(uuid.uuid4()), "bid": str(business_id), "role": "OWNER", "type": "access"}


async def test_active_user_and_active_business_passes():
    business_id = uuid.uuid4()
    db = FakeDB(row=(True, True, None))  # user_active, business_active, suspended_reason

    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)):
        user = await get_current_user(request=MagicMock(), credentials=FakeCredentials(), db=db)

    assert user.business_id == business_id


async def test_suspended_business_is_blocked_even_with_active_user():
    business_id = uuid.uuid4()
    db = FakeDB(row=(True, False, "Payment overdue"))

    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(request=MagicMock(), credentials=FakeCredentials(), db=db)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "BUSINESS_SUSPENDED"
    assert exc.value.detail["details"]["reason"] == "Payment overdue"


async def test_deactivated_user_is_blocked_even_with_active_business():
    business_id = uuid.uuid4()
    db = FakeDB(row=(False, True, None))

    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(request=MagicMock(), credentials=FakeCredentials(), db=db)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ACCOUNT_DISABLED"


async def test_unknown_user_is_blocked():
    business_id = uuid.uuid4()
    db = FakeDB(row=None)

    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(request=MagicMock(), credentials=FakeCredentials(), db=db)

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ACCOUNT_DISABLED"


async def test_missing_credentials_is_401():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(request=MagicMock(), credentials=None, db=FakeDB(row=None))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "MISSING_TOKEN"
