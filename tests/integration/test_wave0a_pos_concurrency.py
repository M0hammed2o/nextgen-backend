"""
Wave 0A — real-Postgres concurrency proofs for the till-open race (BUG-0042)
and the POS checkout idempotency race, plus a real-DB check that the new
`uq_orders_business_id_order_number` constraint actually rejects a
duplicate. Every other Wave 0A fix in this round is covered by mocked unit
tests (see tests/test_till_concurrency.py, tests/test_pos_checkout_idempotency_race.py,
tests/test_pos_refund_void.py, tests/test_payment_status_transitions.py,
tests/test_delivery_fee_vat_recompute.py, tests/test_usage_tracking_commit.py) —
these are specifically the ones a mock can't honestly prove, because the
whole point is two independent DB connections racing against Postgres's own
unique-index enforcement.

Requires TEST_DATABASE_URL to point at a reachable real Postgres already
migrated to at least 0033_order_number_uniqueness (see
docker-compose.test.yml / TESTING.md) — set explicitly when invoking
pytest, e.g.:

  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
  python -m pytest tests/integration/test_wave0a_pos_concurrency.py -q

Fails loudly if TEST_DATABASE_URL isn't set at all (see
tests/integration/_env.py); creates its own uniquely-named business per
test so it is safely re-runnable.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    TillOpenRequest,
    create_pos_order,
    open_till,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from tests.integration._db import get_session_factory
from shared.models.business import Business
from shared.models.order import Order
from shared.models.till import TillSession


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


# ── Group 1: two simultaneous till opens -> exactly one OPEN row ───────────

async def test_two_simultaneous_till_opens_collapse_to_one_open_session(test_business: Business):
    """
    Each attempt gets its OWN AsyncSession/connection (an AsyncSession is
    not safe for concurrent use from two coroutines at once) so the two
    truly race against Postgres, not against each other's Python state.
    """
    business_id = test_business.id
    user = _staff(business_id)

    async def attempt():
        async with get_session_factory()() as session:
            try:
                result = await open_till(
                    body=TillOpenRequest(opening_float_cents=10000), user=user, db=session,
                )
                return ("ok", result.id)
            except AppError as exc:
                return ("conflict", exc.code)
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())
    outcomes = [r[0] for r in results]

    assert outcomes.count("ok") == 1, f"expected exactly one success, got: {results}"
    assert outcomes.count("conflict") == 1, f"expected exactly one 409 conflict, got: {results}"
    conflict = next(r for r in results if r[0] == "conflict")
    assert conflict[1] == "TILL_ALREADY_OPEN"

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(TillSession).where(
                    TillSession.business_id == business_id, TillSession.status == "OPEN",
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "exactly one OPEN till session must survive the race"
        await session.execute(
            select(TillSession).where(TillSession.business_id == business_id)
        )


# ── Group 2: two simultaneous identical checkouts -> exactly one order ─────

async def test_two_simultaneous_identical_checkouts_produce_exactly_one_order(test_business: Business):
    business_id = test_business.id
    owner = AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")
    staff = _staff(business_id)

    async with get_session_factory()() as setup_db:
        category = await create_category(
            body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=setup_db,
        )
        item = await create_item(
            body=ItemCreate(category_id=category.id, name="Race Burger", price_cents=5000),
            user=owner, db=setup_db,
        )
        till = await open_till(body=TillOpenRequest(opening_float_cents=10000), user=staff, db=setup_db)

    idempotency_key = f"race-{uuid.uuid4().hex[:12]}"

    async def attempt():
        async with get_session_factory()() as session:
            body = POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=5000,
                idempotency_key=idempotency_key,
            )
            try:
                order = await create_pos_order(body=body, user=staff, db=session)
                return ("ok", order.id, order.order_number)
            except Exception as exc:  # noqa: BLE001 — proving NEITHER side ever 500s
                return ("error", type(exc).__name__, str(exc))
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())

    # Neither side may ever raise — both must return the SAME order.
    errors = [r for r in results if r[0] == "error"]
    assert errors == [], f"a concurrent idempotent checkout must never raise/500: {errors}"

    order_ids = {r[1] for r in results}
    assert len(order_ids) == 1, f"both requests must resolve to the SAME order id, got: {results}"

    async with get_session_factory()() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.business_id == business_id, Order.idempotency_key == idempotency_key,
                )
            )
        ).scalars().all()
        assert len(orders) == 1, "exactly one order row must exist for this idempotency key"
        assert len(orders[0].items) == 1, "exactly one set of order_items must exist"

    async with get_session_factory()() as cleanup_db:
        from sqlalchemy import delete
        from shared.models.order import OrderEvent, OrderItem
        from shared.models.audit import AuditEvent
        await cleanup_db.execute(delete(OrderItem).where(OrderItem.business_id == business_id))
        await cleanup_db.execute(delete(OrderEvent).where(OrderEvent.business_id == business_id))
        await cleanup_db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
        await cleanup_db.execute(delete(Order).where(Order.business_id == business_id))
        await cleanup_db.execute(delete(TillSession).where(TillSession.business_id == business_id))
        await cleanup_db.commit()


# ── Group 3f: uq_orders_business_id_order_number really rejects a dup ──────

async def test_duplicate_order_number_for_same_business_is_rejected_by_the_db(test_business: Business, db):
    business_id = test_business.id
    order_number = f"BO-DUPTEST-{uuid.uuid4().hex[:6]}"
    common = dict(
        business_id=business_id, order_number=order_number, status="NEW",
        payment_status="UNPAID", order_mode="PICKUP", source="MANUAL",
        subtotal_cents=0, delivery_fee_cents=0, total_cents=0, currency="ZAR",
        discount_cents=0, tax_cents=0,
    )
    db.add(Order(**common))
    await db.commit()

    db.add(Order(**common))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    # A rollback expires every object loaded through this session, including
    # the shared `test_business` fixture's own object — refresh it so the
    # fixture's own teardown (which reads `business.id` to clean up) doesn't
    # trip over an expired-attribute lazy-load outside an active IO context.
    await db.refresh(test_business)
