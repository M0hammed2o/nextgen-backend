"""
Real-Postgres tests for FIX GROUP 1: WhatsApp orders must deduct stock,
exactly once.

Runs against the throwaway local DB pointed to by DATABASE_URL (see
worktree README / task brief) and SKIPS CLEANLY if that database is not
reachable, so this file is safe to collect in any environment:

    ENVIRONMENT=development \
    DATABASE_URL=postgresql+asyncpg://nextgen:wave0a_local_only@localhost:55444/nextgen_test_inv \
    python -m pytest tests/test_inventory_bot_and_cancellation_realdb.py -q

Every fixture uses a uuid4-suffixed business per test (never a shared
tenant), so tests are safe to re-run against the same database any number
of times without colliding with leftover data.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.bot.order_creator import create_order_from_cart
from backend.app.bot.state_machine import add_to_cart
from backend.app.db.session import async_session_factory, engine
from backend.app.inventory.cancellation import reverse_consumption_on_cancel
from backend.app.inventory.ledger import post_sale_consumption, stock_on_hand
from backend.app.inventory.units import to_milli
from shared.enums import OrderStatus
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.inventory import Ingredient
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.recipe import Recipe, RecipeItem
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
        pytest.skip(
            "Real Postgres not reachable at DATABASE_URL — skipping real-DB "
            "inventory tests (see file docstring for how to point this at "
            "the wave0a throwaway DB)."
        )
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def business(db: AsyncSession) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a Inv Test {unique}",
        slug=f"wave0a-inv-{unique}",
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
async def customer(db: AsyncSession, business: Business) -> Customer:
    cust = Customer(business_id=business.id, wa_id=f"27{uuid.uuid4().int % 10**9}")
    db.add(cust)
    await db.commit()
    await db.refresh(cust)
    return cust


@pytest.fixture
async def session(db: AsyncSession, business: Business, customer: Customer) -> ConversationSession:
    sess = ConversationSession(business_id=business.id, customer_id=customer.id, context_json={})
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def _make_ingredient(db, business, name, **overrides):
    defaults = dict(
        business_id=business.id,
        name=name,
        canonical_unit="GRAM",
        count_method="WEIGHT",
        cost_per_canonical_micro_cents=10_000_000,  # R10.00/g, easy arithmetic
    )
    defaults.update(overrides)
    ing = Ingredient(**defaults)
    db.add(ing)
    await db.flush()
    return ing


async def _make_menu_item(db, business, name, price_cents=5000, **overrides):
    defaults = dict(business_id=business.id, name=name, price_cents=price_cents, currency="ZAR")
    defaults.update(overrides)
    item = MenuItem(**defaults)
    db.add(item)
    await db.flush()
    return item


async def _make_recipe(db, business, *, menu_item_id=None, menu_add_on_id=None, yield_qty=1, lines):
    recipe = Recipe(
        business_id=business.id,
        menu_item_id=menu_item_id,
        menu_add_on_id=menu_add_on_id,
        yield_qty=yield_qty,
    )
    db.add(recipe)
    await db.flush()
    for line in lines:
        db.add(RecipeItem(business_id=business.id, recipe_id=recipe.id, **line))
    await db.flush()
    return recipe


def _seed_cart(sess: ConversationSession, item: MenuItem, *, quantity=1, selected_options=None, add_ons=None):
    add_to_cart(
        sess,
        menu_item_id=str(item.id),
        name=item.name,
        price_cents=item.price_cents,
        quantity=quantity,
        selected_options=selected_options,
        add_ons=add_ons,
    )


class TestStandardConsumption:
    async def test_standard_item_deducts_recipe_quantities(self, db, business, customer, session):
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Margherita")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"))],
        )
        _seed_cart(session, pizza, quantity=2)

        order = await create_order_from_cart(db, business, customer, session)
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        # 2 pizzas x 100 g cheese = 200 g removed
        assert positions[cheese.id] == -to_milli(200, "g")

    async def test_inventory_disabled_posts_no_movements(self, db, business, customer, session):
        business.inventory_enabled = False
        await db.commit()
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Margherita")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"))],
        )
        _seed_cart(session, pizza)

        await create_order_from_cart(db, business, customer, session)
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions.get(cheese.id, 0) == 0

    async def test_item_with_no_recipe_deducts_nothing_and_writes_audit_event(
        self, db, business, customer, session
    ):
        burger = await _make_menu_item(db, business, "Mystery Burger (no recipe)")
        _seed_cart(session, burger)

        order = await create_order_from_cart(db, business, customer, session)
        await db.commit()

        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.business_id == business.id,
                    AuditEvent.action == "inventory.sale.missing_recipe",
                    AuditEvent.target_id == order.id,
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert str(burger.id) in events[0].diff_json["menu_item_ids"]


class TestOptionAndAddOnConsumption:
    async def test_size_scoped_recipe_line_deducts_only_the_chosen_size(
        self, db, business, customer, session
    ):
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Chicken Pizza")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[
                dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"), option_id="opt-small"),
                dict(ingredient_id=cheese.id, quantity_milli=to_milli(150, "g"), option_id="opt-large"),
            ],
        )
        _seed_cart(
            session, pizza,
            selected_options=[{"group_id": "grp-size", "option_id": "opt-large", "price_delta_cents": 0}],
        )

        await create_order_from_cart(db, business, customer, session)
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions[cheese.id] == -to_milli(150, "g")

    async def test_add_on_recipe_scales_by_addon_qty_times_line_qty(
        self, db, business, customer, session
    ):
        extra_cheese_stock = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Plain Pizza")
        add_on = MenuAddOn(
            business_id=business.id, name="Extra Cheese", price_cents=1000,
            min_qty=0, max_qty=5, default_qty=1,
        )
        db.add(add_on)
        await db.flush()
        await _make_recipe(
            db, business, menu_add_on_id=add_on.id,
            lines=[dict(ingredient_id=extra_cheese_stock.id, quantity_milli=to_milli(50, "g"))],
        )
        _seed_cart(
            session, pizza, quantity=2,
            add_ons=[{"add_on_id": str(add_on.id), "name": "Extra Cheese",
                      "price_cents": 1000, "quantity": 2}],
        )

        await create_order_from_cart(db, business, customer, session)
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[extra_cheese_stock.id])
        # 2 line-item qty x 2 add-on qty x 50g = 200g
        assert positions[extra_cheese_stock.id] == -to_milli(200, "g")


class TestIdempotency:
    async def test_posting_consumption_twice_for_the_same_order_cannot_double_deduct(
        self, db, business, customer, session
    ):
        """
        The ledger's per-(order, ingredient) idempotency key is a unique
        constraint, not an application-level check — a second attempt to
        post consumption for an order that already has it must fail to
        commit, so the ledger can never end up with two deductions for one
        sale (the true duplicate-webhook case is instead prevented earlier,
        by the wa_message_id unique constraint rolling back the whole
        duplicate order — see order_creator.py / pipeline.py).
        """
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Margherita")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"))],
        )
        _seed_cart(session, pizza)
        order = await create_order_from_cart(db, business, customer, session)
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions[cheese.id] == -to_milli(100, "g")

        from backend.app.inventory.ledger import build_line_specs_from_bot_cart

        specs = build_line_specs_from_bot_cart([
            {"menu_item_id": str(pizza.id), "quantity": 1, "selected_options": [], "add_ons": []}
        ])
        # A fresh session for the "retry" — a real retry (ARQ redelivery,
        # another worker) would never reuse the first attempt's session
        # either, and reusing one here after a forced IntegrityError trips
        # an unrelated asyncpg/greenlet pool-ping quirk, not anything this
        # test is meant to exercise.
        async with async_session_factory() as db2:
            await post_sale_consumption(
                db2, business_id=business.id, order_id=order.id, line_specs=specs,
            )
            with pytest.raises(IntegrityError):
                await db2.commit()
            await db2.rollback()

        async with async_session_factory() as db3:
            positions = await stock_on_hand(db3, business.id, ingredient_ids=[cheese.id])
            assert positions[cheese.id] == -to_milli(100, "g"), (
                "a second post for the same order must not leave a second deduction behind"
            )


class TestCancellationReversal:
    async def _order_with_consumption(self, db, business, customer, session, cheese, pizza):
        _seed_cart(session, pizza)
        order = await create_order_from_cart(db, business, customer, session)
        await db.commit()
        return order

    async def test_cancel_before_preparation_fully_reverses_and_is_idempotent(
        self, db, business, customer, session
    ):
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Margherita")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"))],
        )
        order = await self._order_with_consumption(db, business, customer, session, cheese, pizza)

        await reverse_consumption_on_cancel(
            db, business_id=business.id, inventory_enabled=True,
            order_id=order.id, previous_status=OrderStatus.NEW.value,
        )
        await db.commit()
        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions[cheese.id] == 0, "pre-preparation cancel must net stock back to zero"

        # Second call (double-cancel / retried job) must be a no-op.
        await reverse_consumption_on_cancel(
            db, business_id=business.id, inventory_enabled=True,
            order_id=order.id, previous_status=OrderStatus.NEW.value,
        )
        await db.commit()
        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions[cheese.id] == 0, "reversing an already-reversed order must not overshoot"

    async def test_cancel_after_in_progress_keeps_the_deduction_and_writes_audit_event(
        self, db, business, customer, session
    ):
        cheese = await _make_ingredient(db, business, f"Cheese {uuid.uuid4().hex[:6]}")
        pizza = await _make_menu_item(db, business, "Margherita")
        await _make_recipe(
            db, business, menu_item_id=pizza.id,
            lines=[dict(ingredient_id=cheese.id, quantity_milli=to_milli(100, "g"))],
        )
        order = await self._order_with_consumption(db, business, customer, session, cheese, pizza)

        await reverse_consumption_on_cancel(
            db, business_id=business.id, inventory_enabled=True,
            order_id=order.id, previous_status=OrderStatus.IN_PROGRESS.value,
        )
        await db.commit()

        positions = await stock_on_hand(db, business.id, ingredient_ids=[cheese.id])
        assert positions[cheese.id] == -to_milli(100, "g"), "food was made — stock must stay consumed"

        events = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.business_id == business.id,
                    AuditEvent.action == "inventory.sale.stock_kept_consumed",
                    AuditEvent.target_id == order.id,
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].diff_json["previous_status"] == OrderStatus.IN_PROGRESS.value

    async def test_reversal_is_a_no_op_when_inventory_disabled(
        self, db, business, customer, session
    ):
        # Nothing was ever posted for a disabled tenant, so this must be a
        # true no-op — not an error, not an audit event.
        fake_order_id = uuid.uuid4()
        await reverse_consumption_on_cancel(
            db, business_id=business.id, inventory_enabled=False,
            order_id=fake_order_id, previous_status=OrderStatus.NEW.value,
        )
        await db.commit()
        events = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.target_id == fake_order_id)
            )
        ).scalars().all()
        assert events == []
