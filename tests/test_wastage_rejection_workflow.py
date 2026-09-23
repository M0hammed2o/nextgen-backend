"""
Wastage rejection / genuine review queue (BUG-0083 closure, 2026-09-23).

Verified against the actual backend before this change (not just the
frontend's in-code comment, which is stale — see KNOWN_BUGS.md BUG-0083):
routes_wastage.py already saved an over-threshold STAFF entry PENDING
(approved_by_user_id left null) rather than hard-rejecting it, as of
commit 7d07acd. What was still genuinely missing was the other half of a
review queue: a manager could approve a pending entry, but had no way to
REJECT one — and the response never surfaced enough detail (related order,
evidence photo) to review it properly.

Design decisions under test:
  - The stock movement posts at SUBMISSION time (record_wastage), not at
    approval time — unchanged by this work. Rejecting therefore REVERSES
    that movement rather than skipping a deduction that was never made.
  - The reversal's idempotency key is scoped per (entry, ingredient), so
    the ledger's partial unique index — not application bookkeeping alone
    — makes "at most one reversal, ever" hold even under a race.
  - An approved entry cannot be rejected, and vice versa (both 409).
  - A repeat reject on an already-rejected entry is a no-op: no second
    reversal, same state returned.
  - list_wastage(pending_only=True) is the actual queue view.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.routes_wastage import (
    WastageRejectRequest,
    approve_wastage,
    list_wastage,
    reject_wastage,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.inventory import StockMovement
from shared.models.wastage import WastageEntry


class FakeResult:
    def __init__(self, scalar=None, rows=None, first=None):
        self._scalar = scalar
        self._rows = rows or []
        self._first = first

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows

    def first(self):
        return self._first


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


def make_user(role="MANAGER", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(),
                     role=role, token_type="access")


def make_business(business_id, threshold_cents=1000, inventory_enabled=True):
    import types
    return types.SimpleNamespace(
        id=business_id, inventory_enabled=inventory_enabled,
        wastage_approval_threshold_cents=threshold_cents,
    )


def make_ingredient(business_id, cost_micro_cents=100_000_000):
    import types
    return types.SimpleNamespace(
        id=uuid.uuid4(), business_id=business_id, name="Flour",
        canonical_unit="GRAM", unit_label="g",
        cost_per_canonical_micro_cents=cost_micro_cents, is_deleted=False,
    )


def make_pending_entry(business_id, ingredient_id, value_cents=5000):
    return WastageEntry(
        id=uuid.uuid4(), business_id=business_id, ingredient_id=ingredient_id,
        quantity_milli=50000, entered_quantity="50", entered_unit="g", reason="DROPPED",
        value_cents=value_cents, created_by_user_id=uuid.uuid4(), approved_by_user_id=None,
        occurred_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )


def make_original_movement(business_id, ingredient_id, quantity_milli=-50000, unit_cost=100_000_000):
    return StockMovement(
        id=uuid.uuid4(), business_id=business_id, ingredient_id=ingredient_id,
        quantity_milli=quantity_milli, movement_type="WASTAGE",
        source_type="WASTAGE_ENTRY", unit_cost_micro_cents=unit_cost,
        value_cents=-2500, idempotency_key=f"WASTAGE:x:{ingredient_id}",
        occurred_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )


def make_integrity_error() -> IntegrityError:
    return IntegrityError("INSERT INTO stock_movements ...", {}, Exception(
        'duplicate key value violates unique constraint '
        '"uq_stock_movements_business_id_idempotency_key"'
    ))


def _new_movements(db):
    return [o for o in db.added if isinstance(o, StockMovement)]


# ── reject_wastage: reverses the original movement exactly once ────────────

async def test_reject_reverses_original_movement_with_deterministic_key():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)
    original = make_original_movement(manager.business_id, ingredient.id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),  # entry lookup
        FakeResult(scalar=original),  # original movement lookup
        FakeResult(rows=[]),  # name map
        FakeResult(rows=[]),  # photo map (skipped, but keep list generic)
    ])

    result = await reject_wastage(
        entry_id=entry.id, body=WastageRejectRequest(reason="Staff mis-scanned the weight"),
        user=manager, db=db,
    )

    assert result.status == "REJECTED"
    assert result.rejected_by_user_id == manager.user_id
    assert result.rejection_reason == "Staff mis-scanned the weight"
    movements = _new_movements(db)
    assert len(movements) == 1
    reversal = movements[0]
    assert reversal.movement_type == "WASTAGE_REVERSAL"
    assert reversal.quantity_milli == 50000  # negates the original's -50000
    assert reversal.idempotency_key == f"WASTAGE_REVERSAL:{entry.id}:{ingredient.id}"
    assert entry.rejected_at is not None
    assert db.commits == 1


async def test_reject_already_approved_entry_is_409_no_reversal():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)
    entry.approved_by_user_id = uuid.uuid4()  # already signed off

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
    ])

    with pytest.raises(AppError) as exc:
        await reject_wastage(
            entry_id=entry.id, body=WastageRejectRequest(reason="Too late"),
            user=manager, db=db,
        )
    assert exc.value.code == "WASTAGE_ALREADY_APPROVED"
    assert _new_movements(db) == []
    assert db.commits == 0


async def test_double_reject_is_idempotent_no_second_reversal():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)
    entry.rejected_by_user_id = uuid.uuid4()  # already rejected by someone
    entry.rejected_at = datetime.now(timezone.utc)
    entry.rejection_reason = "Original reason"

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
        FakeResult(rows=[]),  # name map
        FakeResult(rows=[]),  # photo map
    ])

    result = await reject_wastage(
        entry_id=entry.id, body=WastageRejectRequest(reason="Trying again"),
        user=manager, db=db,
    )

    assert result.status == "REJECTED"
    assert result.rejection_reason == "Original reason"  # unchanged
    assert _new_movements(db) == []  # no second reversal
    assert db.commits == 0


async def test_reject_race_caught_by_unique_index_returns_committed_state():
    """Two concurrent reject calls both pass the in-memory checks; the
    loser's commit hits the ledger's unique index and reloads the winner's
    already-committed state instead of erroring or double-reversing."""
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)
    original = make_original_movement(manager.business_id, ingredient.id)

    winner_entry = make_pending_entry(manager.business_id, ingredient.id)
    winner_entry.id = entry.id
    winner_entry.rejected_by_user_id = uuid.uuid4()
    winner_entry.rejected_at = datetime.now(timezone.utc)
    winner_entry.rejection_reason = "Winner's reason"

    db = FakeDB(
        [
            FakeResult(scalar=business),
            FakeResult(first=(entry, ingredient)),  # initial lookup: still pending
            FakeResult(scalar=original),  # original movement lookup
            FakeResult(first=(winner_entry, ingredient)),  # reload after IntegrityError
            FakeResult(rows=[]),  # name map
            FakeResult(rows=[]),  # photo map
        ],
        commit_exc=make_integrity_error(),
    )

    result = await reject_wastage(
        entry_id=entry.id, body=WastageRejectRequest(reason="Loser's reason"),
        user=manager, db=db,
    )

    assert result.rejection_reason == "Winner's reason"
    assert db.rollbacks == 1
    assert db.commits == 0


async def test_reject_missing_movement_refuses_rather_than_silently_reject():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
        FakeResult(scalar=None),  # no original movement found (data anomaly)
    ])

    with pytest.raises(AppError) as exc:
        await reject_wastage(
            entry_id=entry.id, body=WastageRejectRequest(reason="Whatever"),
            user=manager, db=db,
        )
    assert exc.value.code == "WASTAGE_MOVEMENT_MISSING"
    assert entry.rejected_by_user_id is None  # not marked rejected either


async def test_reject_unknown_entry_is_404():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    db = FakeDB([FakeResult(scalar=business), FakeResult(first=None)])

    with pytest.raises(NotFoundError):
        await reject_wastage(
            entry_id=uuid.uuid4(), body=WastageRejectRequest(reason="N/A"),
            user=manager, db=db,
        )


# ── approve_wastage: cannot approve a rejected entry ────────────────────────

async def test_approve_rejected_entry_is_409():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    ingredient = make_ingredient(manager.business_id)
    entry = make_pending_entry(manager.business_id, ingredient.id)
    entry.rejected_by_user_id = uuid.uuid4()

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
    ])

    with pytest.raises(AppError) as exc:
        await approve_wastage(entry_id=entry.id, user=manager, db=db)
    assert exc.value.code == "WASTAGE_ALREADY_REJECTED"


# ── list_wastage(pending_only=True): the actual review queue ───────────────

async def test_pending_only_returns_empty_list_when_no_threshold_configured():
    user = make_user(role="OWNER")
    business = make_business(user.business_id, threshold_cents=0)
    db = FakeDB([FakeResult(scalar=business)])

    result = await list_wastage(limit=100, since=None, pending_only=True, user=user, db=db)

    assert result == []


async def test_pending_only_filters_to_unreviewed_over_threshold_entries():
    user = make_user(role="OWNER")
    business = make_business(user.business_id, threshold_cents=1000)
    ingredient = make_ingredient(user.business_id)
    pending = make_pending_entry(user.business_id, ingredient.id, value_cents=5000)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(rows=[(pending, ingredient)]),
        FakeResult(rows=[]),  # name map
        FakeResult(rows=[]),  # photo map
    ])

    results = await list_wastage(limit=100, since=None, pending_only=True, user=user, db=db)

    assert len(results) == 1
    assert results[0].status == "PENDING_APPROVAL"


# ── Response surfaces evidence for review ───────────────────────────────────

async def test_response_carries_related_order_and_photo_fields():
    user = make_user(role="OWNER")
    business = make_business(user.business_id, threshold_cents=1000)
    ingredient = make_ingredient(user.business_id)
    order_id = uuid.uuid4()
    entry = make_pending_entry(user.business_id, ingredient.id)
    entry.related_order_id = order_id
    entry.photo_asset_id = None  # no photo on this one — url must be None, not error

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(rows=[(entry, ingredient)]),
        FakeResult(rows=[]),  # name map
        FakeResult(rows=[]),  # photo map (empty ids -> skipped anyway)
    ])

    results = await list_wastage(limit=100, since=None, pending_only=False, user=user, db=db)

    assert results[0].related_order_id == order_id
    assert results[0].photo_url is None
