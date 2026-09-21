"""
Wastage approval workflow fix (Wave 0A P1).

Before this fix, record_wastage REJECTED (403) a STAFF entry valued at or
above Business.wastage_approval_threshold_cents — the stock was already
physically gone, so the loss went completely unrecorded (worse than an
unapproved entry: it shows up as unexplained variance instead). OWNER/
MANAGER entries above threshold were auto-approved at creation, so an
approval queue could never exist in practice.

Fixed workflow:
  - STAFF entry >= threshold: SAVED with approved_by_user_id left null
    (pending), and the stock movement posts immediately (the loss already
    happened) — exactly one movement, idempotency-keyed per entry.
  - OWNER/MANAGER entry >= threshold: still auto-approved by the person
    who logged it, one movement.
  - approve_wastage never posts a second movement; it is now also
    idempotent (a second call is a no-op, doesn't reassign the approver).
  - list/approve now derive requires_approval from the business's current
    threshold (previously list_wastage wrongly equated "requires_approval"
    with "already approved", which hid pending entries).
  - WastageResponse now carries logged_by_name/approved_by_name.
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_wastage import (
    WastageCreateRequest,
    approve_wastage,
    list_wastage,
    record_wastage,
)
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser
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
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

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
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="STAFF", business_id=None, user_id=None) -> AuthUser:
    return AuthUser(
        user_id=user_id or uuid.uuid4(),
        business_id=business_id or uuid.uuid4(),
        role=role,
        token_type="access",
    )


def make_business(business_id, threshold_cents=1000, inventory_enabled=True):
    import types
    return types.SimpleNamespace(
        id=business_id,
        inventory_enabled=inventory_enabled,
        wastage_approval_threshold_cents=threshold_cents,
    )


def make_ingredient(business_id, cost_micro_cents=100_000_000):  # R1.00/canonical unit
    import types
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=business_id,
        name="Flour",
        canonical_unit="GRAM",
        unit_label="g",
        cost_per_canonical_micro_cents=cost_micro_cents,
        is_deleted=False,
    )


def _movements(db: FakeDB):
    from shared.models.inventory import StockMovement
    return [obj for obj in db.added if isinstance(obj, StockMovement)]


# ── record_wastage: STAFF over threshold is saved pending, not rejected ────

async def test_staff_over_threshold_is_saved_pending_with_one_movement():
    staff = make_user(role="STAFF")
    business = make_business(staff.business_id, threshold_cents=1000)
    # 50g at R1.00/g canonical cost = 5000 cents, well above 1000c threshold
    ingredient = make_ingredient(staff.business_id, cost_micro_cents=100_000_000)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(rows=[]),  # name-map lookup at the end
    ])

    body = WastageCreateRequest(
        ingredient_id=ingredient.id, quantity="50", unit="g", reason="DROPPED",
    )
    result = await record_wastage(body=body, user=staff, db=db)

    assert result.requires_approval is True
    assert result.approved_by_user_id is None  # pending, not rejected
    assert db.commits == 1
    assert len(_movements(db)) == 1
    movement = _movements(db)[0]
    assert movement.idempotency_key is not None
    assert str(result.id) in movement.idempotency_key


async def test_manager_over_threshold_is_auto_approved_with_one_movement():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id, threshold_cents=1000)
    ingredient = make_ingredient(manager.business_id, cost_micro_cents=100_000_000)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(rows=[(manager.user_id, "Manager Mo")]),
    ])

    body = WastageCreateRequest(ingredient_id=ingredient.id, quantity="50", unit="g", reason="DROPPED")
    result = await record_wastage(body=body, user=manager, db=db)

    assert result.requires_approval is True
    assert result.approved_by_user_id == manager.user_id
    assert result.logged_by_name == "Manager Mo"
    assert result.approved_by_name == "Manager Mo"
    assert len(_movements(db)) == 1


async def test_staff_under_threshold_unchanged():
    staff = make_user(role="STAFF")
    business = make_business(staff.business_id, threshold_cents=1_000_000)  # very high
    ingredient = make_ingredient(staff.business_id, cost_micro_cents=100_000_000)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=ingredient),
        FakeResult(rows=[]),
    ])

    body = WastageCreateRequest(ingredient_id=ingredient.id, quantity="1", unit="g", reason="DROPPED")
    result = await record_wastage(body=body, user=staff, db=db)

    assert result.requires_approval is False
    assert result.approved_by_user_id is None
    assert len(_movements(db)) == 1


# ── approve_wastage: sign-off, exactly-once ledger, idempotent ─────────────

async def test_approve_sets_approver_without_posting_a_second_movement():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id, threshold_cents=1000)
    ingredient = make_ingredient(manager.business_id)
    entry = WastageEntry(
        id=uuid.uuid4(), business_id=manager.business_id, ingredient_id=ingredient.id,
        quantity_milli=50000, entered_quantity="50", entered_unit="g", reason="DROPPED",
        value_cents=5000, created_by_user_id=uuid.uuid4(), approved_by_user_id=None,
        occurred_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
        FakeResult(rows=[(manager.user_id, "Manager Mo")]),
    ])

    result = await approve_wastage(entry_id=entry.id, user=manager, db=db)

    assert result.approved_by_user_id == manager.user_id
    assert result.approved_by_name == "Manager Mo"
    assert db.commits == 1
    assert _movements(db) == []  # no movement posted by approval


async def test_double_approve_is_idempotent():
    """A second approve call on an already-approved entry is a no-op —
    it must not reassign the approver or write again."""
    first_approver = uuid.uuid4()
    second_approver = make_user(role="OWNER")
    business = make_business(second_approver.business_id, threshold_cents=1000)
    ingredient = make_ingredient(second_approver.business_id)
    entry = WastageEntry(
        id=uuid.uuid4(), business_id=second_approver.business_id, ingredient_id=ingredient.id,
        quantity_milli=50000, entered_quantity="50", entered_unit="g", reason="DROPPED",
        value_cents=5000, created_by_user_id=uuid.uuid4(), approved_by_user_id=first_approver,
        occurred_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(first=(entry, ingredient)),
        FakeResult(rows=[]),
    ])

    result = await approve_wastage(entry_id=entry.id, user=second_approver, db=db)

    assert result.approved_by_user_id == first_approver  # unchanged
    assert db.commits == 0  # no write happened
    assert _movements(db) == []


async def test_approve_unknown_entry_is_404():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    db = FakeDB([FakeResult(scalar=business), FakeResult(first=None)])

    with pytest.raises(NotFoundError):
        await approve_wastage(entry_id=uuid.uuid4(), user=manager, db=db)


# ── list_wastage: requires_approval reflects "needs sign-off", not "has it" ─

async def test_list_wastage_shows_pending_entry_as_requiring_approval():
    user = make_user(role="OWNER")
    business = make_business(user.business_id, threshold_cents=1000)
    ingredient = make_ingredient(user.business_id)
    pending_entry = WastageEntry(
        id=uuid.uuid4(), business_id=user.business_id, ingredient_id=ingredient.id,
        quantity_milli=50000, entered_quantity="50", entered_unit="g", reason="DROPPED",
        value_cents=5000, created_by_user_id=uuid.uuid4(), approved_by_user_id=None,
        occurred_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(rows=[(pending_entry, ingredient)]),
        FakeResult(rows=[]),
    ])

    results = await list_wastage(limit=100, since=None, user=user, db=db)

    assert len(results) == 1
    # Before the fix this was False (bug: conflated with "already approved")
    assert results[0].requires_approval is True
    assert results[0].approved_by_user_id is None
