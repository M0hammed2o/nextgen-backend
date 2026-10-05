"""
Goods-receipt idempotency — real-Postgres proof (BUG-0112, migration 0052).

Before this fix, `goods_receipts` had NO idempotency mechanism at all — no
column, no constraint, no client-facing field — unlike `stock_movements`
(migration 0025) or `orders` (migration 0023). A double-click, a client
retry after an apparent timeout, or two tabs submitting the same delivery
each created a fully independent GoodsReceipt, silently doubling both the
recorded quantity and cost. These tests read back the actual rows via raw
SQL against a real, migrated database, and — for the genuine-concurrency
case — fire two truly independent DB connections at Postgres's own unique
index, not two coroutines sharing one connection/transaction, following the
precedent in tests/integration/test_loyalty_concurrency.py /
test_wave0a_pos_concurrency.py.

Requires TEST_DATABASE_URL (see tests/integration/conftest.py /
TESTING.md), already migrated to at least 0052_goods_receipt_idempotency.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_inventory import (
    GoodsReceiptCreateRequest,
    GoodsReceiptLineRequest,
    create_goods_receipt,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.inventory import GoodsReceipt, Ingredient
from tests.integration._db import get_session_factory


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


async def _make_ingredient(db: AsyncSession, business: Business, **overrides) -> Ingredient:
    defaults = dict(
        business_id=business.id,
        name=f"Test Flour {uuid.uuid4().hex[:8]}",
        canonical_unit="GRAM",
        cost_per_canonical_micro_cents=0,
        purchase_pack_milli=1000,
    )
    defaults.update(overrides)
    ingredient = Ingredient(**defaults)
    db.add(ingredient)
    await db.commit()
    await db.refresh(ingredient)
    return ingredient


@pytest.fixture
async def inventory_business(test_business: Business, db: AsyncSession) -> Business:
    test_business.inventory_enabled = True
    db.add(test_business)
    await db.commit()
    await db.refresh(test_business)
    return test_business


async def _goods_receipt_count(business_id: uuid.UUID) -> int:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text("SELECT COUNT(*) FROM goods_receipts WHERE business_id = :bid"),
                {"bid": str(business_id)},
            )
        ).scalar_one()
        return int(row)


# ── Normal submit: one receipt, R500.00 across all three tables ───────────

async def test_normal_submit_creates_exactly_one_receipt_with_correct_r500(
    inventory_business: Business, db: AsyncSession,
):
    ingredient = await _make_ingredient(db, inventory_business, cost_per_canonical_micro_cents=1)
    user = _staff(inventory_business.id)

    body = GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
        )],
        idempotency_key=f"normal-{uuid.uuid4().hex[:12]}",
    )
    result = await create_goods_receipt(body=body, user=user, db=db)
    receipt_id = uuid.UUID(result["id"])

    assert result["total_cost_cents"] == 50000

    # Raw-SQL readback across all three tables (BUG-0111's re-verification
    # requirement, repeated here to confirm BUG-0112's fix didn't disturb
    # the costing semantics).
    async with get_session_factory()() as verify_db:
        receipt_row = (
            await verify_db.execute(
                text("SELECT total_cost_cents FROM goods_receipts WHERE id = :id"),
                {"id": str(receipt_id)},
            )
        ).mappings().one()
        line_row = (
            await verify_db.execute(
                text("SELECT line_cost_cents, unit_cost_cents, quantity_milli "
                     "FROM goods_receipt_lines WHERE goods_receipt_id = :id"),
                {"id": str(receipt_id)},
            )
        ).mappings().one()
        movement_row = (
            await verify_db.execute(
                text("SELECT value_cents FROM stock_movements "
                     "WHERE source_type = 'GOODS_RECEIPT' AND source_id = :id"),
                {"id": str(receipt_id)},
            )
        ).mappings().one()

    assert receipt_row["total_cost_cents"] == 50000
    assert line_row["line_cost_cents"] == 50000
    assert line_row["unit_cost_cents"] == 10000
    assert line_row["quantity_milli"] == 5_000_000
    assert movement_row["value_cents"] == 50000


# ── Double-click / exact retry with the same key ───────────────────────────

async def test_double_click_with_same_key_creates_only_one_receipt(
    inventory_business: Business, db: AsyncSession,
):
    ingredient = await _make_ingredient(db, inventory_business)
    user = _staff(inventory_business.id)
    key = f"double-click-{uuid.uuid4().hex[:12]}"

    def make_body():
        return GoodsReceiptCreateRequest(
            lines=[GoodsReceiptLineRequest(
                ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
            )],
            idempotency_key=key,
        )

    before = await _goods_receipt_count(inventory_business.id)
    first = await create_goods_receipt(body=make_body(), user=user, db=db)
    second = await create_goods_receipt(body=make_body(), user=user, db=db)
    after = await _goods_receipt_count(inventory_business.id)

    assert first["id"] == second["id"]
    assert first["total_cost_cents"] == second["total_cost_cents"] == 50000
    assert after - before == 1, "a double-click must create exactly one receipt, not two"


# ── Timeout-then-retry: same mechanism, framed as the client losing the
#    response after the server already committed ──────────────────────────

async def test_timeout_then_retry_returns_the_original_receipt_not_a_second_one(
    inventory_business: Business, db: AsyncSession,
):
    """
    Simulates: the server commits successfully, but the response never
    reaches the client (connectivity drop) — exactly the scenario named in
    BUG-0112. The retry, using a FRESH session (as a real retried HTTP
    request would), must return the original receipt.
    """
    ingredient = await _make_ingredient(db, inventory_business)
    user = _staff(inventory_business.id)
    key = f"timeout-retry-{uuid.uuid4().hex[:12]}"

    body = GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
        )],
        idempotency_key=key,
    )
    original = await create_goods_receipt(body=body, user=user, db=db)

    async with get_session_factory()() as retry_session:
        retry_body = GoodsReceiptCreateRequest(
            lines=[GoodsReceiptLineRequest(
                ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
            )],
            idempotency_key=key,
        )
        retried = await create_goods_receipt(body=retry_body, user=user, db=retry_session)
        await retry_session.rollback()

    assert retried == original

    count = await _goods_receipt_count(inventory_business.id)
    async with get_session_factory()() as verify_db:
        rows = (
            await verify_db.execute(
                select(GoodsReceipt).where(GoodsReceipt.idempotency_key == key)
            )
        ).scalars().all()
        assert len(rows) == 1, "exactly one receipt must exist for this key, ever"


# ── Genuinely concurrent identical requests: two independent connections ──

async def test_two_simultaneous_identical_submissions_produce_exactly_one_receipt(
    inventory_business: Business, db: AsyncSession,
):
    """
    Each attempt gets its OWN AsyncSession/connection — an AsyncSession is
    not safe for concurrent use from two coroutines at once — so the two
    genuinely race against Postgres's own partial unique index
    (uq_goods_receipts_business_idempotency_key), not against each other's
    Python state in a single process. Follows
    tests/integration/test_wave0a_pos_concurrency.py's
    test_two_simultaneous_identical_checkouts_produce_exactly_one_order
    precedent exactly.
    """
    business_id = inventory_business.id
    ingredient = await _make_ingredient(db, inventory_business)
    user = _staff(business_id)
    key = f"race-{uuid.uuid4().hex[:12]}"

    async def attempt():
        async with get_session_factory()() as session:
            body = GoodsReceiptCreateRequest(
                lines=[GoodsReceiptLineRequest(
                    ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
                )],
                idempotency_key=key,
            )
            try:
                result = await create_goods_receipt(body=body, user=user, db=session)
                return ("ok", result["id"], result["total_cost_cents"])
            except Exception as exc:  # noqa: BLE001 — proving NEITHER side ever 500s
                return ("error", type(exc).__name__, str(exc))
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())

    errors = [r for r in results if r[0] == "error"]
    assert errors == [], f"a concurrent idempotent goods-receipt submission must never raise/500: {errors}"

    receipt_ids = {r[1] for r in results}
    assert len(receipt_ids) == 1, f"both requests must resolve to the SAME receipt id, got: {results}"
    assert all(r[2] == 50000 for r in results), f"both must report the same total, got: {results}"

    async with get_session_factory()() as verify_db:
        receipt_rows = (
            await verify_db.execute(
                select(GoodsReceipt).where(GoodsReceipt.idempotency_key == key)
            )
        ).scalars().all()
        assert len(receipt_rows) == 1, "exactly one GoodsReceipt row must exist for this key"

        # Stock quantity changed exactly once: exactly one GOODS_RECEIVED
        # movement for this receipt, not two.
        movement_count = (
            await verify_db.execute(
                text(
                    "SELECT COUNT(*) FROM stock_movements "
                    "WHERE source_type = 'GOODS_RECEIPT' AND source_id = :id"
                ),
                {"id": str(receipt_rows[0].id)},
            )
        ).scalar_one()
        assert movement_count == 1, "stock quantity must change exactly once, not twice"

        # Financial value changed exactly once.
        value_sum = (
            await verify_db.execute(
                text(
                    "SELECT COALESCE(SUM(value_cents), 0) FROM stock_movements "
                    "WHERE source_type = 'GOODS_RECEIPT' AND source_id = :id"
                ),
                {"id": str(receipt_rows[0].id)},
            )
        ).scalar_one()
        assert value_sum == 50000, "financial value must change exactly once (R500.00), not twice"

        line_count = (
            await verify_db.execute(
                text("SELECT COUNT(*) FROM goods_receipt_lines WHERE goods_receipt_id = :id"),
                {"id": str(receipt_rows[0].id)},
            )
        ).scalar_one()
        assert line_count == 1, "exactly one receipt line must exist, not two"


# ── Same key from a different tenant: must be independent, not a collision ─

async def test_same_idempotency_key_from_a_different_tenant_is_independent(
    inventory_business: Business, db: AsyncSession,
):
    business_a = inventory_business
    ingredient_a = await _make_ingredient(db, business_a)
    user_a = _staff(business_a.id)

    async with get_session_factory()() as setup_db:
        from shared.utils import generate_business_code
        business_b = Business(
            name=f"Integration Test Restaurant B {uuid.uuid4().hex[:8]}",
            slug=f"integration-test-b-{uuid.uuid4().hex[:8]}",
            business_code=generate_business_code(),
            currency="ZAR", timezone="Africa/Johannesburg", inventory_enabled=True,
        )
        setup_db.add(business_b)
        await setup_db.commit()
        await setup_db.refresh(business_b)
        ingredient_b = await _make_ingredient(setup_db, business_b)

    user_b = _staff(business_b.id)
    shared_key = f"shared-key-{uuid.uuid4().hex[:12]}"

    try:
        body_a = GoodsReceiptCreateRequest(
            lines=[GoodsReceiptLineRequest(
                ingredient_id=ingredient_a.id, quantity="5", unit="KG", unit_cost_cents=10000,
            )],
            idempotency_key=shared_key,
        )
        result_a = await create_goods_receipt(body=body_a, user=user_a, db=db)

        async with get_session_factory()() as session_b:
            body_b = GoodsReceiptCreateRequest(
                lines=[GoodsReceiptLineRequest(
                    ingredient_id=ingredient_b.id, quantity="3", unit="KG", unit_cost_cents=20000,
                )],
                idempotency_key=shared_key,
            )
            result_b = await create_goods_receipt(body=body_b, user=user_b, db=session_b)

        # Two genuinely independent receipts, not a collision/replay —
        # tenant isolation on the idempotency key itself.
        assert result_a["id"] != result_b["id"]
        assert result_a["total_cost_cents"] == 50000
        assert result_b["total_cost_cents"] == 60000

        async with get_session_factory()() as verify_db:
            rows = (
                await verify_db.execute(
                    select(GoodsReceipt).where(GoodsReceipt.idempotency_key == shared_key)
                )
            ).scalars().all()
            assert len(rows) == 2, "the same key from two different businesses must both persist"
            assert {str(r.business_id) for r in rows} == {str(business_a.id), str(business_b.id)}
    finally:
        async with get_session_factory()() as cleanup_db:
            from sqlalchemy import delete
            from shared.models.audit import AuditEvent
            await cleanup_db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_b.id))
            await cleanup_db.execute(delete(Business).where(Business.id == business_b.id))
            await cleanup_db.commit()


# ── Same key, materially different payload ─────────────────────────────────

async def test_same_key_different_payload_returns_original_not_a_second_receipt(
    inventory_business: Business, db: AsyncSession,
):
    """
    DECISION (DECISIONS.md): same convention as create_pos_order — replay
    the ORIGINAL receipt rather than reject with a conflict. A client that
    reuses a key across two genuinely different deliveries gets back the
    first delivery's receipt (silently, from its point of view), not a
    second receipt and not an error. The warning-on-mismatch behaviour
    itself is covered by the mocked unit test
    (tests/test_goods_receipt_idempotency.py); this proves the real-DB
    persistence side: no second row is ever created.
    """
    ingredient = await _make_ingredient(db, inventory_business)
    user = _staff(inventory_business.id)
    key = f"mismatched-payload-{uuid.uuid4().hex[:12]}"

    first_body = GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
        )],
        idempotency_key=key,
    )
    first = await create_goods_receipt(body=first_body, user=user, db=db)

    # A materially different delivery (different quantity/cost) reusing the
    # SAME key — this is the early-replay path (sequential, not a race), so
    # per create_pos_order's own early-check convention, it is never even
    # validated against the new payload; it returns the original outright.
    second_body = GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="50", unit="KG", unit_cost_cents=99999,
        )],
        idempotency_key=key,
    )
    second = await create_goods_receipt(body=second_body, user=user, db=db)

    assert second == first, "a same-key-different-payload retry must return the ORIGINAL receipt"

    async with get_session_factory()() as verify_db:
        rows = (
            await verify_db.execute(
                select(GoodsReceipt).where(GoodsReceipt.idempotency_key == key)
            )
        ).scalars().all()
        assert len(rows) == 1, "a mismatched-payload retry must never create a second receipt"
        assert rows[0].total_cost_cents == 50000, "the ORIGINAL delivery's total must be unchanged"


# ── Failure before commit: nothing persisted ───────────────────────────────

async def test_failure_before_commit_persists_nothing(
    inventory_business: Business, db: AsyncSession,
):
    business_id = inventory_business.id
    before = await _goods_receipt_count(business_id)
    user = _staff(business_id)

    body = GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=uuid.uuid4(),  # never created -> UNKNOWN_INGREDIENT, 422
            quantity="5", unit="KG", unit_cost_cents=10000,
        )],
        idempotency_key=f"will-fail-{uuid.uuid4().hex[:12]}",
    )
    with pytest.raises(AppError) as excinfo:
        await create_goods_receipt(body=body, user=user, db=db)
    assert excinfo.value.code == "UNKNOWN_INGREDIENT"

    # No explicit db.rollback() here: the UNKNOWN_INGREDIENT check happens
    # before any db.add()/flush() in create_goods_receipt, so nothing was
    # ever staged — calling rollback() anyway would expire every ORM
    # object still attached to this session, including the inventory_
    # business/test_business fixture's own Business row, and the LATER
    # fixture-teardown code that reads business.id would then trip
    # SQLAlchemy's MissingGreenlet (an expired-attribute reload outside an
    # awaited context) — a test-harness pitfall, not a production concern.
    after = await _goods_receipt_count(business_id)
    assert after == before, "a validation failure before commit must leave zero rows behind"
