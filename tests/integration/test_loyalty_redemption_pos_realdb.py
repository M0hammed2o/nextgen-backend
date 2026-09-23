"""
Real-Postgres proofs for POS loyalty-reward redemption (migration 0050) —
specifically the scenarios a mocked unit test cannot honestly prove, because
the whole point is either two independent DB connections racing against
Postgres's own row lock/unique index, or a genuine end-to-end atomic-
transaction guarantee. Follows the same style/precedent as
tests/integration/test_wave0a_pos_concurrency.py and
tests/integration/test_loyalty_concurrency.py.

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md):

  docker compose -f docker-compose.test.yml up -d
  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
  python -m alembic upgrade head
  python -m pytest tests/integration/test_loyalty_redemption_pos_realdb.py -q
"""
import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, update

from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    TillOpenRequest,
    create_pos_order,
    open_till,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.till import TillSession
from shared.models.audit import AuditEvent
from shared.utils import generate_business_code
from tests.integration._db import get_session_factory


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _enable_loyalty(business_id):
    """
    migration 0051, KNOWN_BUGS.md BUG-0107 — create_pos_order now refuses a
    loyalty_reward_id checkout for a business without Business.loyalty_enabled,
    checked before its own per-program LOYALTY_DISABLED check. test_business
    (conftest.py) defaults it False like every other entitlement flag, so
    every test below that redeems a reward must entitle its business first,
    same as it already explicitly creates the LoyaltyProgram row.
    """
    async with get_session_factory()() as setup_db:
        await setup_db.execute(update(Business).where(Business.id == business_id).values(loyalty_enabled=True))
        await setup_db.commit()


async def _setup_menu_and_till(business_id, owner, price_cents=10000):
    async with get_session_factory()() as setup_db:
        category = await create_category(
            body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=setup_db,
        )
        item = await create_item(
            body=ItemCreate(category_id=category.id, name="Loyalty Test Burger", price_cents=price_cents),
            user=owner, db=setup_db,
        )
        till = await open_till(body=TillOpenRequest(opening_float_cents=10000), user=owner, db=setup_db)
    return item, till


async def _cleanup(business_id):
    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(OrderItem).where(OrderItem.business_id == business_id))
        await cleanup_db.execute(delete(OrderEvent).where(OrderEvent.business_id == business_id))
        await cleanup_db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
        await cleanup_db.execute(delete(Order).where(Order.business_id == business_id))
        await cleanup_db.execute(delete(TillSession).where(TillSession.business_id == business_id))
        await cleanup_db.execute(delete(LoyaltyTransaction).where(LoyaltyTransaction.business_id == business_id))
        await cleanup_db.execute(delete(LoyaltyReward).where(LoyaltyReward.business_id == business_id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == business_id))
        await cleanup_db.execute(delete(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id))
        from shared.models.menu import MenuCategory, MenuItem
        await cleanup_db.execute(delete(MenuItem).where(MenuItem.business_id == business_id))
        await cleanup_db.execute(delete(MenuCategory).where(MenuCategory.business_id == business_id))
        await cleanup_db.commit()


# ── Two concurrent redemptions of a balance sufficient for exactly one ─────

async def test_two_simultaneous_checkout_redemptions_of_a_balance_sufficient_for_one(test_business: Business):
    business_id = test_business.id
    await _enable_loyalty(business_id)
    owner = _owner(business_id)
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=100000)  # R1000 item, plenty of room

    async with get_session_factory()() as setup_db:
        # points_per_rand=0/points_per_order=0 deliberately: the winning
        # order's own EARN step (post-discount total is still R950, per
        # DECISION #2) would otherwise legitimately top the balance back up
        # moments after the redemption commits, letting a genuinely-later
        # second attempt succeed for a real, non-buggy reason — that would
        # test the earn/redeem interaction, not the thing this test exists
        # to prove (two redemptions racing the SAME starting balance).
        program = LoyaltyProgram(
            business_id=business_id, is_enabled=True, min_redemption_points=0,
            points_per_rand=0, points_per_order=0,
        )
        reward = LoyaltyReward(
            business_id=business_id, name="R50 Off", points_cost=100, is_active=True, value_cents=5000,
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821112222",
            loyalty_points_balance=100, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)

    async def attempt():
        async with get_session_factory()() as session:
            body = POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=100000,
                customer_phone="0821112222",
                loyalty_reward_id=reward.id,
                idempotency_key=f"race-{uuid.uuid4().hex[:10]}",
            )
            try:
                order = await create_pos_order(body=body, user=owner, db=session)
                return ("ok", order.id)
            except AppError as exc:
                return ("refused", exc.code)
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())
    outcomes = [r[0] for r in results]

    assert outcomes.count("ok") == 1, f"expected exactly one successful redemption, got: {results}"
    assert outcomes.count("refused") == 1, f"expected exactly one refusal, got: {results}"
    refused = next(r for r in results if r[0] == "refused")
    assert refused[1] == "INSUFFICIENT_POINTS", f"wrong refusal reason: {results}"

    async with get_session_factory()() as verify_db:
        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 0, (
            f"balance must be exactly 0 after one 100-point redemption from a "
            f"100-point balance, got {final_customer.loyalty_points_balance}"
        )
        assert final_customer.loyalty_points_balance >= 0, "balance must never go negative"

        redeem_txns = (
            await verify_db.execute(
                select(LoyaltyTransaction).where(
                    LoyaltyTransaction.customer_id == customer.id,
                    LoyaltyTransaction.transaction_type == "REDEEM",
                )
            )
        ).scalars().all()
        assert len(redeem_txns) == 1, f"exactly one REDEEM transaction must exist, got {len(redeem_txns)}"

    await _cleanup(business_id)


# ── Double-tap with the same idempotency key -> one order, one ledger row ──

async def test_double_tapped_redemption_checkout_produces_one_order_one_ledger_row_one_deduction(
    test_business: Business,
):
    business_id = test_business.id
    await _enable_loyalty(business_id)
    owner = _owner(business_id)
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=10000)

    async with get_session_factory()() as setup_db:
        # points_per_rand=0/points_per_order=0 — isolates the idempotency
        # assertion below (exactly one deduction) from the separate earn
        # step, same reasoning as the concurrency test above.
        program = LoyaltyProgram(
            business_id=business_id, is_enabled=True, min_redemption_points=0,
            points_per_rand=0, points_per_order=0,
        )
        reward = LoyaltyReward(
            business_id=business_id, name="R20 Off", points_cost=50, is_active=True, value_cents=2000,
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821113333",
            loyalty_points_balance=500, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)
        await setup_db.refresh(customer)

    idempotency_key = f"redeem-dbltap-{uuid.uuid4().hex[:10]}"

    async def attempt():
        async with get_session_factory()() as session:
            body = POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=10000,
                customer_phone="0821113333",
                loyalty_reward_id=reward.id,
                idempotency_key=idempotency_key,
            )
            try:
                order = await create_pos_order(body=body, user=owner, db=session)
                return ("ok", order.id)
            except Exception as exc:  # noqa: BLE001 — proving neither side ever 500s
                return ("error", type(exc).__name__, str(exc))
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())
    errors = [r for r in results if r[0] == "error"]
    assert errors == [], f"a concurrent idempotent redemption checkout must never raise/500: {errors}"

    order_ids = {r[1] for r in results}
    assert len(order_ids) == 1, f"both requests must resolve to the SAME order id, got: {results}"

    async with get_session_factory()() as verify_db:
        orders = (
            await verify_db.execute(
                select(Order).where(
                    Order.business_id == business_id, Order.idempotency_key == idempotency_key,
                )
            )
        ).scalars().all()
        assert len(orders) == 1, "exactly one order row must exist for this idempotency key"

        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 450, (
            f"exactly one 50-point deduction must have happened, got balance {final_customer.loyalty_points_balance}"
        )

        redeem_txns = (
            await verify_db.execute(
                select(LoyaltyTransaction).where(
                    LoyaltyTransaction.customer_id == customer.id,
                    LoyaltyTransaction.transaction_type == "REDEEM",
                )
            )
        ).scalars().all()
        assert len(redeem_txns) == 1, f"exactly one REDEEM ledger row must exist, got {len(redeem_txns)}"

    await _cleanup(business_id)


# ── Reward value exceeds order total: clamp + full points cost, real DB ────

async def test_reward_value_exceeding_sale_clamps_discount_and_deducts_full_points_realdb(test_business: Business):
    business_id = test_business.id
    await _enable_loyalty(business_id)
    owner = _owner(business_id)
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=3000)  # R30 item

    async with get_session_factory()() as setup_db:
        program = LoyaltyProgram(business_id=business_id, is_enabled=True, min_redemption_points=0)
        reward = LoyaltyReward(
            business_id=business_id, name="R100 Off", points_cost=300, is_active=True, value_cents=10000,
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821114444",
            loyalty_points_balance=1000, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)
        await setup_db.refresh(customer)

    async with get_session_factory()() as session:
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
            payment_method="CARD",
            customer_phone="0821114444",
            loyalty_reward_id=reward.id,
            idempotency_key=f"clamp-{uuid.uuid4().hex[:10]}",
        )
        order = await create_pos_order(body=body, user=owner, db=session)

    assert order.loyalty_discount_cents == 3000  # clamped to the R30 subtotal
    assert order.loyalty_points_redeemed == 300  # full points cost, not pro-rated
    assert order.total_cents == 0

    async with get_session_factory()() as verify_db:
        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 700  # 1000 - 300

    await _cleanup(business_id)


# ── Reward deactivated between selection and checkout -> 422, nothing moved ─

async def test_reward_deactivated_before_checkout_is_rejected_and_moves_nothing_realdb(test_business: Business):
    business_id = test_business.id
    await _enable_loyalty(business_id)
    owner = _owner(business_id)
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=5000)

    async with get_session_factory()() as setup_db:
        program = LoyaltyProgram(business_id=business_id, is_enabled=True, min_redemption_points=0)
        reward = LoyaltyReward(
            business_id=business_id, name="Now Inactive", points_cost=100,
            is_active=False, value_cents=2000,  # deactivated
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821115555",
            loyalty_points_balance=500, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)
        await setup_db.refresh(customer)

    async with get_session_factory()() as session:
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
            payment_method="CARD",
            customer_phone="0821115555",
            loyalty_reward_id=reward.id,
            idempotency_key=f"inactive-{uuid.uuid4().hex[:10]}",
        )
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=owner, db=session)
        assert exc.value.code == "REWARD_INACTIVE"
        assert exc.value.status_code == 422

    async with get_session_factory()() as verify_db:
        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 500  # untouched

        orders = (
            await verify_db.execute(select(Order).where(Order.business_id == business_id))
        ).scalars().all()
        assert orders == [], "no order (and no order-number burn) from a rejected redemption"

    await _cleanup(business_id)


# ── Tenant isolation: a reward from business B is invisible to business A ──

async def test_reward_from_another_business_is_rejected_not_found(test_business: Business):
    business_a_id = test_business.id
    await _enable_loyalty(business_a_id)
    owner_a = _owner(business_a_id)
    item_a, till_a = await _setup_menu_and_till(business_a_id, owner_a, price_cents=5000)

    unique = uuid.uuid4().hex[:8]
    async with get_session_factory()() as setup_db:
        business_b = Business(
            name=f"Integration Test Restaurant B {unique}",
            slug=f"integration-test-b-{unique}",
            business_code=generate_business_code(),
            currency="ZAR",
            timezone="Africa/Johannesburg",
        )
        setup_db.add(business_b)
        await setup_db.commit()
        await setup_db.refresh(business_b)
    business_b_id = business_b.id

    async with get_session_factory()() as setup_db:
        program_a = LoyaltyProgram(business_id=business_a_id, is_enabled=True, min_redemption_points=0)
        program_b = LoyaltyProgram(business_id=business_b_id, is_enabled=True, min_redemption_points=0)
        reward_b = LoyaltyReward(
            business_id=business_b_id, name="Business B's Reward", points_cost=50,
            is_active=True, value_cents=1000,
        )
        customer_a = Customer(
            business_id=business_a_id, wa_id=None, phone_number="+27821116666",
            loyalty_points_balance=500, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program_a, program_b, reward_b, customer_a])
        await setup_db.commit()
        await setup_db.refresh(reward_b)

    async with get_session_factory()() as session:
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item_a.id, quantity=1)],
            payment_method="CARD",
            customer_phone="0821116666",
            loyalty_reward_id=reward_b.id,  # belongs to business B
            idempotency_key=f"cross-tenant-{uuid.uuid4().hex[:10]}",
        )
        with pytest.raises(NotFoundError):
            await create_pos_order(body=body, user=owner_a, db=session)

    async with get_session_factory()() as verify_db:
        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer_a.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 500  # untouched

    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(LoyaltyReward).where(LoyaltyReward.business_id == business_b_id))
        await cleanup_db.execute(delete(LoyaltyProgram).where(LoyaltyProgram.business_id == business_b_id))
        await cleanup_db.execute(delete(Business).where(Business.id == business_b_id))
        await cleanup_db.commit()

    await _cleanup(business_a_id)
