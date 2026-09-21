"""
FIX GROUP 3 — tiny correctness items:

  * routes_recipes.py: recipe cost must divide by yield_qty (consumption
    already does; costing didn't, so a batch recipe showed N x its real
    per-portion cost).
  * routes_wastage.py: GET "" and POST /{id}/approve were missing the
    inventory_enabled entitlement gate every other inventory route applies.
"""
from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_recipes import _build_response
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.utils import generate_business_code


# ── Recipe costing (pure function, no DB needed) ─────────────────────────────

def _ingredient(cost_per_g_cents=10, name="Cheese"):
    return types.SimpleNamespace(
        name=name,
        canonical_unit="GRAM",
        unit_label=None,
        cost_per_canonical_micro_cents=cost_per_g_cents * 1_000_000,
    )


def _recipe_line(ingredient_id, quantity_milli, option_id=None, sort_order=0):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        ingredient_id=ingredient_id,
        quantity_milli=quantity_milli,
        option_group_id=None,
        option_id=option_id,
        removable_ref_id=None,
        sort_order=sort_order,
    )


def _recipe(items, yield_qty=1):
    return types.SimpleNamespace(
        id=uuid.uuid4(), menu_item_id=uuid.uuid4(), menu_add_on_id=None,
        version=1, is_active=True, yield_qty=yield_qty, notes=None, items=items,
    )


class TestRecipeCostDividesByYield:
    def test_single_portion_recipe_cost_is_unaffected(self):
        """yield_qty=1 (the overwhelming majority of recipes) must be unchanged."""
        cheese_id = uuid.uuid4()
        recipe = _recipe([_recipe_line(cheese_id, 100_000)], yield_qty=1)  # 100 g
        response = _build_response(recipe, {cheese_id: _ingredient(cost_per_g_cents=10)})
        # 100 g x 10c/g = 1000c
        assert response.base_food_cost_cents == 1000

    def test_batch_recipe_cost_is_divided_by_yield_not_multiplied(self):
        """
        A dough batch using 2000 g of flour that yields 4 pizzas must cost
        500 g worth per pizza, not the full 2000 g — this was the bug: the
        route showed 4x the real per-portion cost for every batch recipe.
        """
        flour_id = uuid.uuid4()
        recipe = _recipe([_recipe_line(flour_id, 2_000_000)], yield_qty=4)  # 2000 g batch
        response = _build_response(recipe, {flour_id: _ingredient(cost_per_g_cents=2)})
        # 2000g / 4 = 500g per portion; 500g x 2c/g = 1000c
        assert response.base_food_cost_cents == 1000

    def test_quantity_milli_on_the_line_itself_still_shows_the_full_batch_amount(self):
        """Only the cost is scaled — the displayed recipe quantity is what was entered."""
        flour_id = uuid.uuid4()
        recipe = _recipe([_recipe_line(flour_id, 2_000_000)], yield_qty=4)
        response = _build_response(recipe, {flour_id: _ingredient(cost_per_g_cents=2)})
        assert response.items[0].quantity_milli == 2_000_000

    def test_option_scoped_line_cost_also_divides_by_yield(self):
        cheese_id = uuid.uuid4()
        recipe = _recipe(
            [_recipe_line(cheese_id, 600_000, option_id="opt-large")], yield_qty=3
        )
        response = _build_response(recipe, {cheese_id: _ingredient(cost_per_g_cents=5)})
        # 600g / 3 = 200g per portion; 200g x 5c/g = 1000c, attributed to the option not base
        assert response.base_food_cost_cents == 0
        assert response.max_food_cost_cents == 1000

    def test_rounding_matches_consumptions_half_up_rule(self):
        """
        An odd batch (e.g. 100g split three ways) must round the same way
        consumption.py's _scale does, so cost and actual ledger deduction
        never silently disagree for the same recipe.
        """
        salt_id = uuid.uuid4()
        recipe = _recipe([_recipe_line(salt_id, 100_000)], yield_qty=3)  # 100g / 3 = 33.33g
        response = _build_response(recipe, {salt_id: _ingredient(cost_per_g_cents=1)})
        # 100_000 / 3 = 33333.33... -> rounds to 33333 milli-g = 33.333g -> 33c (half-up on cents)
        assert response.base_food_cost_cents == 33


# ── Wastage entitlement gate ─────────────────────────────────────────────────

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
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping real-DB wastage tests")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def business_without_inventory(db: AsyncSession) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a No-Inventory Test {unique}",
        slug=f"wave0a-noinv-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        inventory_enabled=False,
    )
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    yield biz
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == biz.id))
    await db.execute(delete(Business).where(Business.id == biz.id))
    await db.commit()


def _user(business_id: uuid.UUID, role: str = "MANAGER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


class TestWastageEntitlementGate:
    async def test_list_wastage_is_gated_by_inventory_enabled(
        self, db: AsyncSession, business_without_inventory: Business
    ):
        from backend.app.api.v1.routes_wastage import list_wastage

        with pytest.raises(AppError) as exc_info:
            await list_wastage(limit=100, since=None, user=_user(business_without_inventory.id), db=db)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "INVENTORY_NOT_ENABLED"

    async def test_approve_wastage_is_gated_by_inventory_enabled(
        self, db: AsyncSession, business_without_inventory: Business
    ):
        from backend.app.api.v1.routes_wastage import approve_wastage

        with pytest.raises(AppError) as exc_info:
            await approve_wastage(
                uuid.uuid4(), user=_user(business_without_inventory.id), db=db
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "INVENTORY_NOT_ENABLED"
