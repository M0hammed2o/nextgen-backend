"""
Goods-receipt idempotency (BUG-0112, migration 0052).

Mirrors tests/test_pos_checkout_idempotency_race.py's shape exactly: these
are mocked unit tests proving the catch/replay logic in isolation (the early
replay lookup, the partial-unique-index collision handling, and the
same-key-different-payload decision). The genuine two-connections-racing-
against-a-real-Postgres proof lives in
tests/integration/test_goods_receipt_idempotency_realdb.py.

Before this fix, `goods_receipts` had NO idempotency mechanism at all — no
column, no constraint, no client-facing field — unlike `stock_movements`
(`uq_stock_movements_business_id_idempotency_key`, migration 0025) or
`orders` (`uq_orders_business_idempotency_key`, migration 0023). A
double-click, a client retry after an apparent timeout, or two tabs
submitting the same delivery each created a fully independent GoodsReceipt,
silently doubling both the recorded quantity and cost.
"""
import logging
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1 import routes_inventory
from backend.app.api.v1.routes_inventory import (
    GoodsReceiptCreateRequest,
    GoodsReceiptLineRequest,
    _is_goods_receipt_idempotency_violation,
    create_goods_receipt,
)
from backend.app.core.rbac import AuthUser


# ── Fakes (mirrors tests/test_pos_checkout_idempotency_race.py's FakeDB) ───

class FakeResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_list or []

        class _Scalars:
            def all(_self):
                return items

        return _Scalars()


class FakeDB:
    """
    Like test_inventory_goods_receipt_costing.py's FakeDB, but `flush()` can
    be made to raise once — simulating the moment Postgres actually enforces
    the partial unique index on (business_id, idempotency_key), which
    happens at INSERT time (i.e. at this ORM's `db.flush()`), not
    necessarily at the final commit.
    """
    def __init__(self, results, flush_exc=None):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._flush_exc = flush_exc
        self._flush_calls = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now

    async def flush(self):
        self._flush_calls += 1
        if self._flush_exc is not None and self._flush_calls == 1:
            raise self._flush_exc

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="STAFF", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(),
                     role=role, token_type="access")


def make_business(business_id, inventory_enabled=True):
    return types.SimpleNamespace(id=business_id, inventory_enabled=inventory_enabled)


def make_ingredient(business_id, canonical_unit="GRAM", cost_micro_cents=50_000_000,
                     purchase_pack_milli=1000):
    return types.SimpleNamespace(
        id=uuid.uuid4(), business_id=business_id, name="Mozzarella",
        canonical_unit=canonical_unit, unit_label=None,
        cost_per_canonical_micro_cents=cost_micro_cents,
        purchase_pack_milli=purchase_pack_milli, is_deleted=False,
    )


def make_existing_receipt(receipt_id=None, total_cost_cents=50000, line_count=1):
    return types.SimpleNamespace(
        id=receipt_id or uuid.uuid4(),
        total_cost_cents=total_cost_cents,
        lines=[object() for _ in range(line_count)],
    )


def make_integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError("INSERT INTO goods_receipts ...", {}, Exception(
        f'duplicate key value violates unique constraint "{constraint_name}"'
    ))


def make_body(key=None, quantity="5", unit="KG", unit_cost_cents=10000, ingredient_id=None):
    return GoodsReceiptCreateRequest(
        lines=[GoodsReceiptLineRequest(
            ingredient_id=ingredient_id or uuid.uuid4(), quantity=quantity, unit=unit,
            unit_cost_cents=unit_cost_cents,
        )],
        idempotency_key=key,
    )


# ── _is_goods_receipt_idempotency_violation helper ──────────────────────────

def test_is_goods_receipt_idempotency_violation_matches_named_constraint():
    exc = make_integrity_error("uq_goods_receipts_business_idempotency_key")
    assert _is_goods_receipt_idempotency_violation(exc) is True


def test_is_goods_receipt_idempotency_violation_does_not_match_other_constraints():
    exc = make_integrity_error("some_other_constraint")
    assert _is_goods_receipt_idempotency_violation(exc) is False


# ── No key at all: old-client compatibility, behaviour unchanged ──────────

async def test_no_idempotency_key_behaves_exactly_as_before():
    """
    An old client that never sends idempotency_key must keep working
    unchanged — the column/field are both optional (nullable / default
    None), mirroring orders.idempotency_key's compatibility shape exactly.
    """
    user = make_user()
    ingredient = make_ingredient(user.business_id)
    business = make_business(user.business_id)
    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalars_list=[ingredient]),
    ])

    body = make_body(key=None, ingredient_id=ingredient.id)
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 50000
    assert db.commits == 1
    assert db.rollbacks == 0


# ── Early replay path: key already committed by a prior request ───────────

async def test_early_replay_returns_existing_receipt_without_any_further_writes():
    """
    A client retry (double-click, or a resubmit after an apparent timeout)
    with the SAME key that a prior, already-successful request used must
    return that original receipt — and must not touch ingredient validation,
    supplier validation, or issue any write at all.
    """
    user = make_user()
    business = make_business(user.business_id)
    existing = make_existing_receipt(total_cost_cents=50000, line_count=2)

    db = FakeDB([
        FakeResult(scalar=business),       # _require_inventory_enabled
        FakeResult(scalar=existing),        # early idempotency replay lookup
    ])

    body = make_body(key="delivery-2026-10-05-001")
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result == {
        "id": str(existing.id), "lines": 2, "total_cost_cents": 50000,
    }
    # Nothing was ever added/committed on the replay path.
    assert db.added == []
    assert db.commits == 0
    assert db.rollbacks == 0


# ── Genuine race: loser's flush raises, replays the winner ────────────────

async def test_concurrent_submission_loser_replays_winner_instead_of_500():
    user = make_user()
    ingredient = make_ingredient(user.business_id)
    business = make_business(user.business_id)
    winner = make_existing_receipt(total_cost_cents=50000, line_count=1)

    exc = make_integrity_error("uq_goods_receipts_business_idempotency_key")
    results = [
        FakeResult(scalar=business),            # _require_inventory_enabled
        FakeResult(scalar=None),                # early lookup: not committed yet
        FakeResult(scalars_list=[ingredient]),  # ingredient lookup
        FakeResult(scalar=winner),              # replay lookup after the race
    ]
    db = FakeDB(results, flush_exc=exc)

    body = make_body(key="delivery-2026-10-05-002", ingredient_id=ingredient.id)
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result == {
        "id": str(winner.id), "lines": 1, "total_cost_cents": 50000,
    }
    assert db.rollbacks == 1
    assert db.commits == 0  # the loser's own transaction never committed


async def test_integrity_error_on_a_different_constraint_is_never_masked():
    """
    A real bug (any OTHER constraint violation) must propagate as a real
    error, never be silently swallowed as if it were an idempotency replay.
    """
    user = make_user()
    ingredient = make_ingredient(user.business_id)
    business = make_business(user.business_id)

    exc = make_integrity_error("fk_goods_receipt_lines_ingredient_id_ingredients")
    results = [
        FakeResult(scalar=business),
        FakeResult(scalar=None),
        FakeResult(scalars_list=[ingredient]),
    ]
    db = FakeDB(results, flush_exc=exc)

    body = make_body(key="delivery-2026-10-05-003", ingredient_id=ingredient.id)
    with pytest.raises(IntegrityError):
        await create_goods_receipt(body=body, user=user, db=db)

    assert db.rollbacks == 1
    assert db.commits == 0


async def test_integrity_error_with_no_key_at_all_always_reraises():
    """Without a key, there is nothing to replay — any IntegrityError at
    all (even one that happens to mention the constraint by coincidence)
    must propagate; the replay path is gated on body.idempotency_key."""
    user = make_user()
    ingredient = make_ingredient(user.business_id)
    business = make_business(user.business_id)

    exc = make_integrity_error("uq_goods_receipts_business_idempotency_key")
    results = [
        FakeResult(scalar=business),
        FakeResult(scalars_list=[ingredient]),
    ]
    db = FakeDB(results, flush_exc=exc)

    body = make_body(key=None, ingredient_id=ingredient.id)
    with pytest.raises(IntegrityError):
        await create_goods_receipt(body=body, user=user, db=db)


# ── Same key, materially different payload: decision = replay + warn ──────

async def test_same_key_different_payload_replays_silently_but_logs_a_warning(caplog):
    """
    DECISION (DECISIONS.md): mirrors create_pos_order's
    _replay_idempotent_checkout exactly — a same-key-different-payload
    collision replays the WINNING receipt (never rejects with a conflict,
    never creates a second receipt) but logs a warning so a real client bug
    (reusing a key across two different deliveries) stays operationally
    visible.
    """
    user = make_user()
    ingredient = make_ingredient(user.business_id)
    business = make_business(user.business_id)
    # Winner has a totally different total/line-count than this attempt.
    winner = make_existing_receipt(total_cost_cents=99900, line_count=3)

    exc = make_integrity_error("uq_goods_receipts_business_idempotency_key")
    results = [
        FakeResult(scalar=business),
        FakeResult(scalar=None),
        FakeResult(scalars_list=[ingredient]),
        FakeResult(scalar=winner),
    ]
    db = FakeDB(results, flush_exc=exc)

    body = make_body(key="reused-key-different-payload", ingredient_id=ingredient.id,
                      quantity="5", unit_cost_cents=10000)  # attempted total = 50000

    with caplog.at_level(logging.WARNING, logger="nextgen"):
        result = await create_goods_receipt(body=body, user=user, db=db)

    # The ORIGINAL (winning) receipt is what comes back — never the second,
    # different-payload attempt's numbers.
    assert result == {"id": str(winner.id), "lines": 3, "total_cost_cents": 99900}
    assert any(
        "different receipt than this request would have created" in rec.message
        for rec in caplog.records
    ), "a same-key-different-payload replay must be logged, not silent"
