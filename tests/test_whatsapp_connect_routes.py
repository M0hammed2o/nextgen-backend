"""
Wave 0A / Milestone C — routes_whatsapp_connect.py: RBAC wiring, module-
disabled gating, GET /config availability, and status response shape.

Permission enforcement itself (require_owner/require_owner_or_manager
rejecting the wrong role) is centrally implemented in backend/app/core/rbac.py
and already covered generically elsewhere — these tests confirm THIS
router wires the CORRECT dependency to each route (connect/disconnect =
OWNER only; status/health/test-message = owner or manager) and directly
exercise require_owner's rejection of STAFF/MANAGER, the same dependency
object the route actually uses.

Uses the real throwaway Postgres DB for the module-disabled/status-shape
tests (skips cleanly if unreachable, same pattern as
tests/test_whatsapp_connections_realdb.py); RBAC wiring tests need no DB.
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import routes_whatsapp_connect as routes
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser, require_owner, require_owner_or_manager
from backend.app.db.session import async_session_factory, engine
from shared.models.business import Business
from shared.utils import generate_business_code


# ── RBAC wiring: each route depends on the correct role gate ───────────────

def _dependency_default(route_func, param_name: str):
    sig = inspect.signature(route_func)
    return sig.parameters[param_name].default


def test_exchange_route_requires_owner():
    dep = _dependency_default(routes.exchange_code, "user")
    assert dep.dependency is require_owner


def test_disconnect_route_requires_owner():
    dep = _dependency_default(routes.disconnect_whatsapp, "user")
    assert dep.dependency is require_owner


def test_status_route_allows_owner_or_manager():
    dep = _dependency_default(routes.get_connection_status, "user")
    assert dep.dependency is require_owner_or_manager


def test_health_check_route_allows_owner_or_manager():
    dep = _dependency_default(routes.run_health_check, "user")
    assert dep.dependency is require_owner_or_manager


def test_test_message_route_allows_owner_or_manager():
    dep = _dependency_default(routes.send_test_message, "user")
    assert dep.dependency is require_owner_or_manager


async def test_require_owner_rejects_staff_and_manager():
    for role in ("STAFF", "MANAGER"):
        user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role=role, token_type="access")
        with pytest.raises(HTTPException) as exc_info:
            await require_owner(user=user)
        assert exc_info.value.status_code == 403


async def test_require_owner_allows_owner():
    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="OWNER", token_type="access")
    result = await require_owner(user=user)
    assert result.role == "OWNER"


async def test_require_owner_or_manager_rejects_staff():
    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="STAFF", token_type="access")
    with pytest.raises(HTTPException) as exc_info:
        await require_owner_or_manager(user=user)
    assert exc_info.value.status_code == 403


# ── GET /config availability ────────────────────────────────────────────────

async def test_connect_config_unavailable_when_unconfigured():
    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="OWNER", token_type="access")
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(routes, "settings") as mock_settings:
        mock_settings.META_APP_ID = ""
        mock_settings.META_ES_CONFIG_ID = ""
        result = await routes.get_connect_config(user=user)
    assert result.available is False
    assert result.app_id is None


async def test_connect_config_available_when_configured():
    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="OWNER", token_type="access")
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(routes, "settings") as mock_settings:
        mock_settings.META_APP_ID = "app-123"
        mock_settings.META_ES_CONFIG_ID = "config-456"
        mock_settings.META_API_VERSION = "v22.0"
        result = await routes.get_connect_config(user=user)
    assert result.available is True
    assert result.app_id == "app-123"
    assert result.config_id == "config-456"


# ── Module-disabled gating + status shape (real DB) ─────────────────────────

async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db() -> AsyncSession:
    if not await _db_reachable():
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping WhatsApp connect route tests.")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a Routes Test {unique}",
        slug=f"wave0a-routes-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
    )
    for k, v in overrides.items():
        setattr(biz, k, v)
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    return biz


async def test_status_route_returns_not_connected_when_no_connection(db):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    result = await routes.get_connection_status(user=user, db=db)
    assert result.connected is False
    assert result.status == "NOT_CONNECTED"


async def test_module_disabled_returns_409_on_status_route(db):
    business = await _make_business(db, is_whatsapp_enabled=False)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    with pytest.raises(AppError) as exc_info:
        await routes.get_connection_status(user=user, db=db)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "WHATSAPP_NOT_ENABLED"


async def test_disconnect_with_no_connection_returns_409(db):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    with pytest.raises(AppError) as exc_info:
        await routes.disconnect_whatsapp(user=user, db=db)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "WHATSAPP_NOT_CONNECTED"
