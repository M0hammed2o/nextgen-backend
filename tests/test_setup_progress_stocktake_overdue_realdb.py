"""
Real-Postgres smoke test: backend/app/api/v1/routes_setup.py::compute_setup_progress
surfaces stocktake_overdue/last_stocktake_at from a real approved
StocktakeSession, end to end (not just the pure is_stocktake_overdue unit
tested in tests/test_stocktake_overdue.py).

Skips cleanly if DATABASE_URL is not reachable — same pattern as
tests/test_stocktake_realdb.py.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_setup import compute_setup_progress
from backend.app.api.v1.routes_stocktake import (
    CountLineRequest,
    CountSubmitRequest,
    StocktakeOpenRequest,
    approve_stocktake,
    complete_stocktake,
    open_stocktake,
    submit_counts,
)
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.utils import generate_business_code


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
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping real-DB test")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def business(db: AsyncSession) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a Overdue Test {unique}",
        slug=f"wave0a-overdue-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        inventory_enabled=True,
        stocktake_frequency="DAILY_1",
    )
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    yield biz
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == biz.id))
    await db.execute(delete(Business).where(Business.id == biz.id))
    await db.commit()


def _user(business_id: uuid.UUID, role: str = "OWNER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


class TestSetupProgressStocktakeOverdue:
    async def test_no_stocktake_ever_on_a_brand_new_business_is_not_overdue(
        self, db: AsyncSession, business: Business,
    ):
        # Business was just created (fixture above) — hasn't had a chance yet.
        progress = await compute_setup_progress(db, business)
        assert progress.inventory_enabled is True
        assert progress.last_stocktake_at is None
        assert progress.stocktake_overdue is False

    async def test_completed_and_approved_stocktake_today_is_not_overdue(
        self, db: AsyncSession, business: Business,
    ):
        ing = Ingredient(
            business_id=business.id, name=f"Test Ingredient {uuid.uuid4().hex[:6]}",
            canonical_unit="GRAM", count_method="WEIGHT",
            cost_per_canonical_micro_cents=1_000_000,
        )
        db.add(ing)
        await db.commit()
        await db.refresh(ing)

        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[CountLineRequest(ingredient_id=ing.id, quantity="1", unit="G")]),
            user=_user(business.id), db=db,
        )
        await complete_stocktake(opened.id, user=_user(business.id), db=db)
        await approve_stocktake(opened.id, user=_user(business.id), db=db)

        progress = await compute_setup_progress(db, business)
        assert progress.last_stocktake_at is not None
        assert progress.stocktake_overdue is False

    async def test_inventory_disabled_business_reports_na_not_false(
        self, db: AsyncSession, business: Business,
    ):
        business.inventory_enabled = False
        await db.commit()
        progress = await compute_setup_progress(db, business)
        assert progress.stocktake_overdue is None
        assert progress.last_stocktake_at is None
