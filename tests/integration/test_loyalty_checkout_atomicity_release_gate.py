"""
Release-gate proof (Part 1 of the release-gate brief): the exact
"balance 1000, R50/500-point reward, R200 cart" scenario the product owner
specified, run against a REAL Postgres — plus the idempotent-retry and
failure-atomicity invariants that make the number trustworthy.

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md):

  docker compose -f docker-compose.test.yml up -d
  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
  python -m alembic upgrade head
  python -m pytest tests/integration/test_loyalty_checkout_atomicity_release_gate.py -q

Follows the same fixture-reuse discipline as
tests/integration/test_loyalty_redemption_pos_realdb.py and
tests/integration/test_loyalty_concurrency.py (same helpers, same
points_per_rand=0/points_per_order=0 isolation trick — see that file's own
comment for exactly why earning must be zeroed to observe redemption alone).
"""
import uuid

import pytest
from sqlalchemy import delete, select, update
from unittest.mock import AsyncMock

from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    TillOpenRequest,
    create_pos_order,
    open_till,
)
from backend.app.core.rbac import AuthUser
from backend.app.receipts.document import build_receipt_document
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.till import TillSession
from shared.models.audit import AuditEvent
from tests.integration._db import get_session_factory


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _enable_loyalty(business_id):
    """
    migration 0051, KNOWN_BUGS.md BUG-0107 — `create_pos_order` now refuses
    a `loyalty_reward_id` checkout for a business without
    `Business.loyalty_enabled`, checked before create_pos_order's own
    per-program LOYALTY_DISABLED check. `test_business` (conftest.py)
    defaults it False like every other entitlement flag, so every test in
    this file that exercises checkout redemption must explicitly entitle
    its business first, same as it already explicitly creates the
    LoyaltyProgram/LoyaltyReward rows below.
    """
    async with get_session_factory()() as setup_db:
        await setup_db.execute(update(Business).where(Business.id == business_id).values(loyalty_enabled=True))
        await setup_db.commit()


async def _setup_menu_and_till(business_id, owner, price_cents):
    async with get_session_factory()() as setup_db:
        category = await create_category(
            body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=setup_db,
        )
        item = await create_item(
            body=ItemCreate(category_id=category.id, name="Release Gate Item", price_cents=price_cents),
            user=owner, db=setup_db,
        )
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=setup_db)
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


# ── Part 1, scenario 1: the exact product-owner numbers, plus a double-tap ──

async def test_release_gate_scenario_1000_balance_500_point_reward_200_cart_then_retried(
    test_business: Business,
):
    """
    Setup: customer balance 1000 points; a reward costing 500 points worth
    R50 (value_cents=5000); a cart totalling R200.

    Expected after checkout: order total R150.00 (15000 cents), customer
    balance 500, the order records the R50 loyalty discount, and the
    receipt document renders the reward line.

    Then the EXACT same checkout request is retried with the SAME
    idempotency key. The result must still be one order, one R50
    redemption, and 500 points remaining -- never two orders, two
    redemptions, or a balance back at 1000/0.
    """
    business_id = test_business.id
    owner = _owner(business_id)
    await _enable_loyalty(business_id)
    # R200 cart -> a single line item priced at exactly 20000 cents.
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=20000)

    async with get_session_factory()() as setup_db:
        # points_per_rand=0/points_per_order=0 deliberately, per
        # test_loyalty_redemption_pos_realdb.py's own documented reasoning:
        # isolates the idempotency/atomicity assertions below from the
        # sale's own separate EARN step, which would otherwise top the
        # balance back up moments after redemption and mask what is being
        # measured here.
        program = LoyaltyProgram(
            business_id=business_id, is_enabled=True, min_redemption_points=0,
            points_per_rand=0, points_per_order=0,
        )
        reward = LoyaltyReward(
            business_id=business_id, name="R50 Off", points_cost=500, is_active=True, value_cents=5000,
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821119999",
            loyalty_points_balance=1000, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)
        await setup_db.refresh(customer)

    idempotency_key = f"release-gate-{uuid.uuid4().hex[:10]}"

    async def _checkout():
        async with get_session_factory()() as session:
            body = POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CARD",
                customer_phone="0821119999",
                loyalty_reward_id=reward.id,
                idempotency_key=idempotency_key,
            )
            return await create_pos_order(body=body, user=owner, db=session)

    # ── First checkout: the exact scenario ───────────────────────────────
    order = await _checkout()

    assert order.total_cents == 15000, f"expected order total R150.00 (15000c), got {order.total_cents}"
    assert order.loyalty_discount_cents == 5000, "the order must record the R50 loyalty discount"
    assert order.loyalty_points_redeemed == 500

    async with get_session_factory()() as verify_db:
        customer_after_first = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert customer_after_first.loyalty_points_balance == 500, (
            f"balance must be exactly 500 (1000 - 500) after redemption, "
            f"got {customer_after_first.loyalty_points_balance}"
        )

        # ── The receipt document renders the reward line ────────────────
        order_row = (
            await verify_db.execute(select(Order).where(Order.id == order.id))
        ).scalar_one()
        order_items = (
            await verify_db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        ).scalars().all()
        business_row = (
            await verify_db.execute(select(Business).where(Business.id == business_id))
        ).scalar_one()
        doc = build_receipt_document(order_row, business_row, order_items=order_items)
        assert doc.totals.loyalty_reward_name == "R50 Off"
        assert doc.totals.loyalty_discount_cents == 5000
        assert doc.totals.loyalty_discount_display == "-R50.00", (
            f"receipt must render the reward discount line, got {doc.totals.loyalty_discount_display!r}"
        )

    # ── Retry: EXACT same request, same idempotency key ──────────────────
    retried_order = await _checkout()
    assert retried_order.id == order.id, "a retried checkout must return the SAME order, not create a new one"
    assert retried_order.total_cents == 15000

    async with get_session_factory()() as verify_db:
        orders = (
            await verify_db.execute(
                select(Order).where(Order.business_id == business_id, Order.idempotency_key == idempotency_key)
            )
        ).scalars().all()
        assert len(orders) == 1, f"exactly one order must exist after the retry, got {len(orders)}"

        redeem_txns = (
            await verify_db.execute(
                select(LoyaltyTransaction).where(
                    LoyaltyTransaction.customer_id == customer.id,
                    LoyaltyTransaction.transaction_type == "REDEEM",
                )
            )
        ).scalars().all()
        assert len(redeem_txns) == 1, f"exactly one R50 redemption must exist after the retry, got {len(redeem_txns)}"

        customer_after_retry = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert customer_after_retry.loyalty_points_balance == 500, (
            f"500 points must remain after the retry (never 1000, never 0), "
            f"got {customer_after_retry.loyalty_points_balance}"
        )

    await _cleanup(business_id)


# ── Part 1, scenario 2: points must never disappear on a failed commit ──────

async def test_failed_transaction_after_points_staged_leaves_balance_and_order_untouched(
    test_business: Business, monkeypatch,
):
    """
    "Points must never disappear" invariant. Forces a failure AFTER the
    redemption's points mutation has been staged (flushed) inside
    create_pos_order's transaction, but BEFORE the final db.commit() that
    would make it durable, by monkeypatching AsyncSession.commit to raise a
    plain (non-IntegrityError, so it is NOT caught by the idempotency-replay
    except clause in routes_pos.py) RuntimeError on its first call.

    The whole transaction -- the points deduction AND the order insert --
    must then roll back together: the customer's balance must be completely
    unchanged and no order may exist. A points ledger that could lose a
    customer's balance on an unrelated failure elsewhere in the same
    checkout would be a much worse bug than the checkout itself failing.
    """
    business_id = test_business.id
    owner = _owner(business_id)
    await _enable_loyalty(business_id)
    item, till = await _setup_menu_and_till(business_id, owner, price_cents=10000)

    async with get_session_factory()() as setup_db:
        program = LoyaltyProgram(
            business_id=business_id, is_enabled=True, min_redemption_points=0,
            points_per_rand=0, points_per_order=0,
        )
        reward = LoyaltyReward(
            business_id=business_id, name="R30 Off", points_cost=300, is_active=True, value_cents=3000,
        )
        customer = Customer(
            business_id=business_id, wa_id=None, phone_number="+27821118888",
            loyalty_points_balance=800, loyalty_tier="BRONZE",
        )
        setup_db.add_all([program, reward, customer])
        await setup_db.commit()
        await setup_db.refresh(reward)
        await setup_db.refresh(customer)

    idempotency_key = f"forced-failure-{uuid.uuid4().hex[:10]}"

    async with get_session_factory()() as session:
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
            payment_method="CARD",
            customer_phone="0821118888",
            loyalty_reward_id=reward.id,
            idempotency_key=idempotency_key,
        )

        # Force the failure right where the checkout would otherwise commit
        # -- after the points mutation, the order row, and the order items
        # have all been flushed into this same open transaction.
        monkeypatch.setattr(
            session, "commit", AsyncMock(side_effect=RuntimeError("forced failure before commit")),
        )

        with pytest.raises(RuntimeError, match="forced failure before commit"):
            await create_pos_order(body=body, user=owner, db=session)

        # The real ROLLBACK (session.commit is mocked, session.rollback is
        # not) discards everything flushed-but-never-committed above --
        # exactly what a crashed process / dropped connection would also do.
        await session.rollback()

    async with get_session_factory()() as verify_db:
        final_customer = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final_customer.loyalty_points_balance == 800, (
            f"the customer's balance must be COMPLETELY UNCHANGED after a failed "
            f"commit -- points must never disappear -- got {final_customer.loyalty_points_balance}"
        )

        orders = (
            await verify_db.execute(
                select(Order).where(Order.business_id == business_id, Order.idempotency_key == idempotency_key)
            )
        ).scalars().all()
        assert orders == [], f"no order may exist after a failed commit, got {len(orders)}"

        redeem_txns = (
            await verify_db.execute(
                select(LoyaltyTransaction).where(LoyaltyTransaction.customer_id == customer.id)
            )
        ).scalars().all()
        assert redeem_txns == [], "no loyalty transaction may exist after a failed commit"

    await _cleanup(business_id)
