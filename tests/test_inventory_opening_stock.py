"""
Opening-stock endpoint (Ciao Pizza gap-closure pass, 2026-09-23).

Before this change the ONLY way to post an OPENING_BALANCE stock movement
was the ingredient CSV importer (routes_import.py, new ingredients only) —
there was no route for a normal owner/manager onboarding an ingredient that
already exists to set its starting stock position.

Behavioural contract under test:
  - Posts through the ledger (StockMovementType.OPENING_BALANCE), never a
    direct mutation — stock stays fully ledger-derived.
  - No separate "opening cost" input: valuation reuses the ingredient's
    already-configured cost_per_canonical_micro_cents, same as every other
    movement type.
  - REJECTED (409), not replaced or added to, on a second attempt for the
    same ingredient — enforced two ways: a pre-check by movement_type (so
    it also catches an opening balance the CSV importer already posted,
    which carries no idempotency key), and the ledger's partial unique
    index on (business_id, idempotency_key) as the race-proof backstop.
  - Audited via AuditEvent, same discipline as the manual adjustment route.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.routes_inventory import OpeningStockRequest, set_opening_stock
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
from shared.models.inventory import StockMovement


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, results, commit_exc=None):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._commit_exc = commit_exc
        self._commit_calls = 0

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
        pass

    async def commit(self):
        self._commit_calls += 1
        if self._commit_exc is not None and self._commit_calls == 1:
            raise self._commit_exc
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="OWNER", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(),
                     role=role, token_type="access")


def make_business(business_id, inventory_enabled=True):
    import types
    return types.SimpleNamespace(id=business_id, inventory_enabled=inventory_enabled)


def make_ingredient(business_id, cost_micro_cents=50_000_000):  # R0.50/canonical unit
    import types
    return types.SimpleNamespace(
        id=uuid.uuid4(), business_id=business_id, name="Mozzarella",
        canonical_unit="GRAM", unit_label=None,
        cost_per_canonical_micro_cents=cost_micro_cents, is_deleted=False,
    )


def make_integrity_error() -> IntegrityError:
    return IntegrityError("INSERT INTO stock_movements ...", {}, Exception(
        'duplicate key value violates unique constraint '
        '"uq_stock_movements_business_id_idempotency_key"'
    ))


def _movements(db):
    return [o for o in db.added if isinstance(o, StockMovement)]


def _audits(db):
    return [o for o in db.added if isinstance(o, AuditEvent)]


# ── Happy path ───────────────────────────────────────────────────────────────

async def test_set_opening_stock_posts_one_movement_no_direct_mutation():
    user = make_user()
    business = make_business(user.business_id)
    ingredient = make_ingredient(user.business_id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(scalar=None),  # pre-check: no existing OPENING_BALANCE
    ])

    body = OpeningStockRequest(ingredient_id=ingredient.id, quantity="10", unit="kg",
                                source="Physical count on go-live day")
    result = await set_opening_stock(body=body, user=user, db=db)

    assert result.quantity_milli == 10_000_000  # 10 kg canonical grams * 1000
    assert result.value_cents == 500_000  # 10_000 g * R0.50/g (50c/g) = R5000 = 500 000c
    movements = _movements(db)
    assert len(movements) == 1
    m = movements[0]
    assert m.movement_type == "OPENING_BALANCE"
    assert m.quantity_milli == 10_000_000
    assert m.idempotency_key == f"OPENING_BALANCE:{ingredient.id}"
    assert m.notes == "Physical count on go-live day"
    assert db.commits == 1
    assert len(_audits(db)) == 1
    assert _audits(db)[0].action == "inventory.stock.opening_balance_set"


async def test_set_opening_stock_no_source_defaults_note():
    user = make_user()
    business = make_business(user.business_id)
    ingredient = make_ingredient(user.business_id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(scalar=None),
    ])

    body = OpeningStockRequest(ingredient_id=ingredient.id, quantity="500", unit="g")
    await set_opening_stock(body=body, user=user, db=db)

    assert _movements(db)[0].notes == "Opening stock"


# ── Rejected on a second attempt (not replaced, not additive) ──────────────

async def test_set_opening_stock_rejected_when_already_set_precheck():
    """Pre-check catches a prior OPENING_BALANCE movement even with no
    idempotency key — e.g. one posted by the CSV ingredient importer."""
    user = make_user()
    business = make_business(user.business_id)
    ingredient = make_ingredient(user.business_id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(scalar=uuid.uuid4()),  # an OPENING_BALANCE movement id already exists
    ])

    body = OpeningStockRequest(ingredient_id=ingredient.id, quantity="10", unit="kg")
    with pytest.raises(AppError) as exc:
        await set_opening_stock(body=body, user=user, db=db)
    assert exc.value.code == "OPENING_STOCK_ALREADY_SET"
    assert _movements(db) == []
    assert db.commits == 0


async def test_set_opening_stock_race_caught_by_unique_index():
    """Two concurrent requests both pass the pre-check; the second's
    commit hits the ledger's partial unique index and is converted to the
    same clean 409, never a raw 500 or a silently-doubled opening balance."""
    user = make_user()
    business = make_business(user.business_id)
    ingredient = make_ingredient(user.business_id)

    db = FakeDB(
        [
            FakeResult(scalar=business),
            FakeResult(scalar=ingredient),
            FakeResult(scalar=None),  # pre-check passes (race window)
        ],
        commit_exc=make_integrity_error(),
    )

    body = OpeningStockRequest(ingredient_id=ingredient.id, quantity="10", unit="kg")
    with pytest.raises(AppError) as exc:
        await set_opening_stock(body=body, user=user, db=db)
    assert exc.value.code == "OPENING_STOCK_ALREADY_SET"
    assert db.rollbacks == 1
    assert db.commits == 0


# ── Validation / not-found ───────────────────────────────────────────────────

async def test_set_opening_stock_unknown_ingredient_is_404():
    user = make_user()
    business = make_business(user.business_id)

    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    body = OpeningStockRequest(ingredient_id=uuid.uuid4(), quantity="10", unit="kg")
    with pytest.raises(NotFoundError):
        await set_opening_stock(body=body, user=user, db=db)


async def test_set_opening_stock_zero_quantity_is_422():
    user = make_user()
    business = make_business(user.business_id)
    ingredient = make_ingredient(user.business_id)

    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=ingredient)])

    body = OpeningStockRequest(ingredient_id=ingredient.id, quantity="0", unit="kg")
    with pytest.raises(AppError) as exc:
        await set_opening_stock(body=body, user=user, db=db)
    assert exc.value.code == "INVALID_QUANTITY"
