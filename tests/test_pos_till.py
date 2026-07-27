"""
Till (cash-up) — pure arithmetic (backend/app/pos/till.py) and the
open/close routes (backend/app/api/v1/routes_pos.py).

Close is a one-way transition: closing an already-closed session is
rejected rather than silently re-mutated, and every close writes a
scope=BUSINESS AuditEvent (the first real business-plane use of that
table's scope, per the Phase 1 POS plan).
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_pos import TillCloseRequest, TillOpenRequest, close_till, get_current_till, open_till
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.pos.till import compute_expected_cash_cents, compute_variance_cents
from shared.models.audit import AuditEvent
from shared.models.till import TillSession


# ── Pure arithmetic ──────────────────────────────────────────────────────────

def test_expected_cash_is_opening_float_plus_cash_sales():
    assert compute_expected_cash_cents(opening_float_cents=1000, cash_sales_cents=5000) == 6000


def test_variance_positive_when_over():
    assert compute_variance_cents(expected_cash_cents=6000, counted_cash_cents=6200) == 200


def test_variance_negative_when_short():
    assert compute_variance_cents(expected_cash_cents=6000, counted_cash_cents=5800) == -200


def test_variance_zero_when_exact():
    assert compute_variance_cents(expected_cash_cents=6000, counted_cash_cents=6000) == 0


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, scalar=None, items=None):
        self._scalar = scalar
        self._items = items

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return FakeScalars(self._items or [])


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.executed_statements = []

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        # Simulate id/opened_at/created_at defaults SQLAlchemy would apply
        # at INSERT time, so response schemas can validate against a
        # never-flushed object.
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "opened_at") and getattr(obj, "opened_at", None) is None:
            obj.opened_at = now
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_session(business_id, status="OPEN", opening_float_cents=10000):
    return TillSession(
        id=uuid.uuid4(),
        business_id=business_id,
        opened_by_user_id=uuid.uuid4(),
        opening_float_cents=opening_float_cents,
        status=status,
        opened_at=datetime.now(timezone.utc),
    )


# ── Current ──────────────────────────────────────────────────────────────────

async def test_get_current_till_returns_none_when_no_session_open():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    result = await get_current_till(user=user, db=db)
    assert result is None


async def test_get_current_till_returns_the_open_session():
    user = make_user()
    session = make_session(user.business_id)
    db = FakeDB([FakeResult(scalar=session)])

    result = await get_current_till(user=user, db=db)
    assert result.id == session.id
    assert result.status == "OPEN"


# ── Open ─────────────────────────────────────────────────────────────────────

async def test_open_till_creates_session_when_none_open():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])  # no existing OPEN session

    result = await open_till(body=TillOpenRequest(opening_float_cents=5000), user=user, db=db)

    assert result.status == "OPEN"
    assert result.opening_float_cents == 5000
    assert db.commits == 1


async def test_open_till_blocked_when_already_open():
    user = make_user()
    existing = make_session(user.business_id)
    db = FakeDB([FakeResult(scalar=existing)])

    with pytest.raises(AppError) as exc:
        await open_till(body=TillOpenRequest(opening_float_cents=5000), user=user, db=db)
    assert exc.value.code == "TILL_ALREADY_OPEN"
    assert exc.value.status_code == 409


# ── Close ────────────────────────────────────────────────────────────────────

async def test_close_till_unknown_session_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await close_till(
            session_id=uuid.uuid4(),
            body=TillCloseRequest(counted_cash_cents=1000),
            user=user, db=db,
        )


async def test_close_till_already_closed_is_rejected():
    user = make_user()
    session = make_session(user.business_id, status="CLOSED")
    db = FakeDB([FakeResult(scalar=session)])

    with pytest.raises(AppError) as exc:
        await close_till(
            session_id=session.id,
            body=TillCloseRequest(counted_cash_cents=1000),
            user=user, db=db,
        )
    assert exc.value.code == "TILL_ALREADY_CLOSED"
    assert exc.value.status_code == 422


async def test_close_till_computes_variance_and_writes_audit_event():
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),           # TillSession lookup
        FakeResult(items=[5000, 3000]),       # Order.total_cents for CASH-paid orders since open
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=18500),
        user=user, db=db,
    )

    # expected = 10000 (float) + 8000 (cash sales) = 18000; counted 18500 -> +500 over
    assert result.expected_cash_cents == 18000
    assert result.variance_cents == 500
    assert result.status == "CLOSED"

    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].scope == "BUSINESS"
    assert audit_events[0].action == "pos.till.closed"
    assert audit_events[0].diff_json["variance_cents"] == 500


async def test_close_till_cash_query_excludes_refunded_orders():
    """
    Regression test (bug found via real-DB integration testing, 2026-07-24):
    the cash-sales query must exclude REFUNDED orders — a cash sale that was
    later refunded is no longer cash sitting in the drawer, so counting its
    total_cents toward `expected_cash_cents` would produce a false shortfall
    at close. This mocked test can't fake a real WHERE-clause bug (the fake
    DB just returns whatever items it's told to), so it asserts the actual
    compiled SQL includes a payment_status filter, closing the gap where the
    same bug could otherwise silently regress unnoticed by mocked tests alone.
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(items=[5000]),
    ])

    await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=15000),
        user=user, db=db,
    )

    cash_query = db.executed_statements[1]
    compiled = str(cash_query.compile(compile_kwargs={"literal_binds": True}))
    assert "payment_status" in compiled
