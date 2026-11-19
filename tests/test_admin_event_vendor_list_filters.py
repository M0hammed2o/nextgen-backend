"""
Query validation of the admin vendor list (GET /admin/events/{id}/vendors).

E2E finding (souk Playwright run 2026-11-19): the admin UI offered
status=SUSPENDED, which the API rejects with 422 because suspension is not an
EventVendor status. The status filter stays strict; suspension is filtered
with the separate boolean `suspended` query parameter. Validation runs before
the handler, so no database is needed to prove the 422s; the accepted case is
proven to reach the handler (the stub session raises a sentinel).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin_api.app.api.v1 import routes_events
from admin_api.app.core.deps import require_super_admin
from backend.app.core.rbac import AuthUser
from backend.app.db.session import get_db


class _Reached(Exception):
    pass


class _StubSession:
    async def get(self, *a, **kw):
        raise _Reached()

    async def execute(self, *a, **kw):
        raise _Reached()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes_events.router)

    async def _admin():
        return AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")

    async def _db():
        yield _StubSession()

    app.dependency_overrides[require_super_admin] = _admin
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=True)


URL = f"/admin/events/{uuid.uuid4()}/vendors"


def test_status_suspended_is_still_rejected(client):
    assert client.get(URL, params={"status": "SUSPENDED"}).status_code == 422


@pytest.mark.parametrize("status", ["PENDING", "ACTIVE", "UNPAID", "EXPIRED", "CANCELLED"])
def test_real_statuses_pass_validation(client, status):
    with pytest.raises(_Reached):
        client.get(URL, params={"status": status})


def test_suspended_flag_must_be_boolean(client):
    assert client.get(URL, params={"suspended": "maybe"}).status_code == 422


@pytest.mark.parametrize("value", ["true", "false"])
def test_suspended_flag_passes_validation(client, value):
    with pytest.raises(_Reached):
        client.get(URL, params={"suspended": value})
