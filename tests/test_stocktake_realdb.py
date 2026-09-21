"""
Real-Postgres tests for FIX GROUP 2: stocktake unit correctness + the three
small same-subsystem fixes (COMPLETED lock, row-locked approval, overlapping
periods).

Skips cleanly if DATABASE_URL is not reachable — see
tests/test_inventory_bot_and_cancellation_realdb.py's docstring for the
exact command to point this at the wave0a throwaway DB. Every test uses a
uuid4-suffixed business, so this file is safe to re-run any number of times.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_stocktake import (
    CountLineRequest,
    CountSubmitRequest,
    StocktakeOpenRequest,
    approve_stocktake,
    complete_stocktake,
    open_stocktake,
    submit_counts,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine
from backend.app.inventory.units import to_milli
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
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping real-DB stocktake tests")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def business(db: AsyncSession) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a Stocktake Test {unique}",
        slug=f"wave0a-stk-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        inventory_enabled=True,
    )
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    yield biz
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == biz.id))
    await db.execute(delete(Business).where(Business.id == biz.id))
    await db.commit()


@pytest.fixture
async def cheese(db: AsyncSession, business: Business) -> Ingredient:
    ing = Ingredient(
        business_id=business.id,
        name=f"Cheese {uuid.uuid4().hex[:6]}",
        canonical_unit="GRAM",
        count_method="WEIGHT",
        cost_per_canonical_micro_cents=10_000_000,
    )
    db.add(ing)
    await db.commit()
    await db.refresh(ing)
    return ing


def _user(business_id: uuid.UUID, role: str = "MANAGER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


class TestUnitRequiredAndValidated:
    async def test_missing_unit_is_rejected_before_it_ever_reaches_the_route(self):
        """
        `unit` is now a required field on CountLineRequest — FastAPI itself
        turns a raised ValidationError here into a 422 naming the missing
        field, before the route body ever executes.
        """
        with pytest.raises(ValidationError):
            CountLineRequest(ingredient_id=uuid.uuid4(), quantity="1.5")

    async def test_kg_to_g_is_exact_integer_milli(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        result = await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[
                CountLineRequest(ingredient_id=cheese.id, quantity="2.5", unit="KG")
            ]),
            user=_user(business.id),
            db=db,
        )
        line = next(l for l in result.lines if l.ingredient_id == cheese.id)
        assert line.counted_quantity_milli == 2_500_000  # 2.5 kg = 2 500 000 milli-g
        assert line.entered_unit == "KG"
        assert line.entered_quantity == "2.5"

    async def test_g_to_g_is_exact(self, db: AsyncSession, business: Business, cheese: Ingredient):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        result = await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[
                CountLineRequest(ingredient_id=cheese.id, quantity="150", unit="G")
            ]),
            user=_user(business.id), db=db,
        )
        line = next(l for l in result.lines if l.ingredient_id == cheese.id)
        assert line.counted_quantity_milli == 150_000

    async def test_cross_family_unit_is_a_clean_4xx_naming_ingredient_and_units(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        """Cheese is GRAM (mass) — counting it in litres (volume) must 422, not 500."""
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        with pytest.raises(AppError) as exc_info:
            await submit_counts(
                opened.id,
                CountSubmitRequest(lines=[
                    CountLineRequest(ingredient_id=cheese.id, quantity="2", unit="L")
                ]),
                user=_user(business.id), db=db,
            )
        assert exc_info.value.status_code in (400, 422)
        assert cheese.name in exc_info.value.message
        assert "L" in exc_info.value.message or "litre" in exc_info.value.message.lower()

    async def test_unknown_unit_is_a_clean_4xx(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        with pytest.raises(AppError) as exc_info:
            await submit_counts(
                opened.id,
                CountSubmitRequest(lines=[
                    CountLineRequest(ingredient_id=cheese.id, quantity="2", unit="FURLONGS")
                ]),
                user=_user(business.id), db=db,
            )
        assert exc_info.value.status_code == 422

    async def test_negative_quantity_is_a_clean_4xx(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        with pytest.raises(AppError) as exc_info:
            await submit_counts(
                opened.id,
                CountSubmitRequest(lines=[
                    CountLineRequest(ingredient_id=cheese.id, quantity="-5", unit="G")
                ]),
                user=_user(business.id), db=db,
            )
        assert exc_info.value.status_code == 422

    async def test_portion_unit_is_accepted_for_a_countable_ingredient(
        self, db: AsyncSession, business: Business
    ):
        box = Ingredient(
            business_id=business.id, name=f"Pizza Box {uuid.uuid4().hex[:6]}",
            canonical_unit="UNIT", count_method="UNIT_COUNT", unit_label="box",
            cost_per_canonical_micro_cents=500_000,
        )
        db.add(box)
        await db.commit()
        await db.refresh(box)

        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        result = await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[
                CountLineRequest(ingredient_id=box.id, quantity="12", unit="BOX")
            ]),
            user=_user(business.id), db=db,
        )
        line = next(l for l in result.lines if l.ingredient_id == box.id)
        assert line.counted_quantity_milli == to_milli(12, "unit")


class TestCompletedLock:
    async def test_counts_cannot_be_edited_after_completion(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[
                CountLineRequest(ingredient_id=cheese.id, quantity="10", unit="G")
            ]),
            user=_user(business.id), db=db,
        )
        await complete_stocktake(opened.id, user=_user(business.id), db=db)

        with pytest.raises(AppError) as exc_info:
            await submit_counts(
                opened.id,
                CountSubmitRequest(lines=[
                    CountLineRequest(ingredient_id=cheese.id, quantity="99", unit="G")
                ]),
                user=_user(business.id), db=db,
            )
        assert exc_info.value.status_code == 409


class TestOverlappingPeriodsBlocked:
    async def test_cannot_open_a_new_draft_while_a_completed_unapproved_one_exists(
        self, db: AsyncSession, business: Business, cheese: Ingredient
    ):
        opened = await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        await submit_counts(
            opened.id,
            CountSubmitRequest(lines=[
                CountLineRequest(ingredient_id=cheese.id, quantity="10", unit="G")
            ]),
            user=_user(business.id), db=db,
        )
        await complete_stocktake(opened.id, user=_user(business.id), db=db)

        with pytest.raises(AppError) as exc_info:
            await open_stocktake(StocktakeOpenRequest(), user=_user(business.id), db=db)
        assert exc_info.value.status_code == 409


class TestConcurrentApproval:
    async def test_two_simultaneous_approvals_post_exactly_one_set_of_adjustments(
        self, business: Business, cheese: Ingredient
    ):
        """
        Two DB sessions racing approve_stocktake for the same session: the
        row lock (with_for_update) serializes them, and the idempotency key
        is the DB-level backstop — either way, adjustments must be posted
        exactly once.
        """
        async with async_session_factory() as setup_db:
            opened = await open_stocktake(
                StocktakeOpenRequest(), user=_user(business.id), db=setup_db
            )
            await submit_counts(
                opened.id,
                CountSubmitRequest(lines=[
                    # Counted 500g against an opening/expected of 0 -> a
                    # non-zero variance, so approval actually has an
                    # adjustment to post.
                    CountLineRequest(ingredient_id=cheese.id, quantity="500", unit="G")
                ]),
                user=_user(business.id), db=setup_db,
            )
            await complete_stocktake(opened.id, user=_user(business.id), db=setup_db)
            session_id = opened.id

        results = []

        async def _approve():
            async with async_session_factory() as db2:
                try:
                    await approve_stocktake(
                        session_id, post_adjustments=True, user=_user(business.id), db=db2
                    )
                    results.append("ok")
                except AppError as exc:
                    results.append(exc.code)

        await asyncio.gather(_approve(), _approve())

        # Exactly one of the two must have succeeded; the other must have
        # hit STOCKTAKE_LOCKED (already approved) once it acquired the lock.
        assert sorted(results) == ["STOCKTAKE_LOCKED", "ok"], results

        async with async_session_factory() as verify_db:
            from backend.app.inventory.ledger import stock_on_hand
            positions = await stock_on_hand(
                verify_db, business.id, ingredient_ids=[cheese.id]
            )
            # Exactly one +500g adjustment posted, not two.
            assert positions[cheese.id] == to_milli(500, "g")
