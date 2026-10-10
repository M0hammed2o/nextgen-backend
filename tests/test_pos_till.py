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


def test_expected_cash_subtracts_cash_refunds():
    assert compute_expected_cash_cents(opening_float_cents=1000, cash_sales_cents=5000, cash_refunds_cents=2000) == 4000


def test_expected_cash_defaults_refunds_to_zero():
    assert compute_expected_cash_cents(opening_float_cents=1000, cash_sales_cents=5000) == \
        compute_expected_cash_cents(opening_float_cents=1000, cash_sales_cents=5000, cash_refunds_cents=0)


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
        FakeResult(scalar=0),                 # allocation-based cash sum (none — legacy orders, no allocation rows)
        FakeResult(items=[5000, 3000]),       # Order.total_cents for CASH-paid orders in this session
        FakeResult(items=[]),                 # Order.refund_cash_cents for refunds/voids in this session
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=18500),
        user=user, db=db,
    )

    # expected = 10000 (float) + 8000 (cash sales) - 0 (refunds) = 18000; counted 18500 -> +500 over
    assert result.expected_cash_cents == 18000
    assert result.variance_cents == 500
    assert result.status == "CLOSED"
    assert result.cash_sales_cents == 8000
    assert result.cash_refunds_cents == 0

    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].scope == "BUSINESS"
    assert audit_events[0].action == "pos.till.closed"
    assert audit_events[0].diff_json["variance_cents"] == 500


async def test_close_till_sums_total_cents_which_is_already_net_of_any_loyalty_reward():
    """
    Pins a claim from the loyalty-redemption change (migration 0050): the
    cash-sales query sums Order.total_cents directly, and total_cents is
    ALREADY net of any redeemed reward's discount by the time checkout wrote
    it — so a R80 cash sale that had a R20 reward applied appears here as
    exactly 8000, not 10000 minus something computed at close time. No code
    change was needed for this to be correct; this test exists so a future
    "fix" doesn't accidentally try to subtract loyalty_discount_cents again
    here (which would double-discount the drawer expectation).
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),      # allocation-based cash sum (none — legacy order, no allocation rows)
        FakeResult(items=[8000]),  # a R100 sale with a R20 reward already netted into total_cents
        FakeResult(items=[]),
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=18000),
        user=user, db=db,
    )
    assert result.expected_cash_cents == 18000  # 10000 float + 8000 (the reward-netted total)
    assert result.cash_sales_cents == 8000


async def test_close_till_cash_query_still_filters_by_payment_status():
    """
    The cash-sales query filters `payment_status IN (PAID, REFUNDED,
    PARTIALLY_REFUNDED, VOIDED)` — deliberately including refunded/voided
    orders too (2026-07-28 fix: see test_close_till_same_session_refund_is_not_double_counted
    for why excluding them here would double-reverse the same refund). This
    just confirms the query still filters on payment_status at all — e.g.
    never counting a still-PENDING/UNPAID cash order that hasn't actually
    been paid yet.
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),  # allocation-based cash sum (none — legacy order, no allocation rows)
        FakeResult(items=[5000]),
        FakeResult(items=[]),
    ])

    await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=15000),
        user=user, db=db,
    )

    # index 2: the legacy Order-column fallback query (index 1 is the new
    # allocation-based query, which has no payment_status filter of its own).
    cash_query = db.executed_statements[2]
    compiled = str(cash_query.compile(compile_kwargs={"literal_binds": True}))
    assert "payment_status" in compiled


async def test_close_till_cash_refund_reduces_expected_cash():
    """Restaurant POS redesign: a cash refund given back during this session
    must reduce expected_cash_cents — the drawer is genuinely lighter by that
    amount, not still holding the original sale's cash."""
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),          # allocation-based cash sum (none — legacy orders, no allocation rows)
        FakeResult(items=[5000]),      # one CASH sale of R50
        FakeResult(items=[2000]),      # one CASH refund of R20 in the same session
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=13000),
        user=user, db=db,
    )

    # expected = 10000 (float) + 5000 (sales) - 2000 (refund) = 13000 -> exact
    assert result.expected_cash_cents == 13000
    assert result.variance_cents == 0
    assert result.cash_sales_cents == 5000
    assert result.cash_refunds_cents == 2000


async def test_close_till_cash_sales_query_links_via_till_session_id():
    """
    The cash-sales query filters on Order.till_session_id (set at POS
    checkout) instead of the old opened_at/paid_at timestamp-window
    heuristic — confirms the compiled query actually references the new
    column, closing the risk of silently reverting to the fragile heuristic.
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),  # allocation-based cash sum (none — legacy order, no allocation rows)
        FakeResult(items=[]),
        FakeResult(items=[]),
    ])

    await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=10000),
        user=user, db=db,
    )

    # index 2: the legacy Order-column fallback query (index 1 is the new
    # allocation-based query, which also filters on till_session_id, but
    # this test's intent is specifically the legacy Order linkage).
    cash_query = db.executed_statements[2]
    assert "till_session_id" in str(cash_query.compile(compile_kwargs={"literal_binds": True}))


async def test_close_till_refund_query_uses_refunded_at_not_till_session_id():
    """
    Regression test for a real double-counting bug found via a live-Postgres
    integration test (2026-07-28): filtering refunds by the ORIGINAL sale's
    till_session_id double-subtracts a same-session refund, since the
    cash-sales query already excludes that order once its payment_status
    moves off PAID. Refunds must instead be scoped by refunded_at falling
    within the currently-closing session's open window.
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),  # allocation-based cash sum (none — legacy order, no allocation rows)
        FakeResult(items=[]),
        FakeResult(items=[]),
    ])

    await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=10000),
        user=user, db=db,
    )

    refund_query = db.executed_statements[3]
    compiled = str(refund_query.compile(compile_kwargs={"literal_binds": True}))
    assert "refunded_at" in compiled
    assert "till_session_id" not in compiled


async def test_close_till_same_session_refund_is_not_double_counted():
    """
    Regression test for a real bug found via a live-Postgres integration
    test (2026-07-28): an R80 cash sale refunded in full in the SAME
    session it was sold in must net to zero effect on the drawer (the R80
    went in, then came back out) — R10000 float stays R10000. An earlier,
    buggy version of this fix excluded refunded orders from cash_sales
    entirely (double-reversing the refund: -R80 sales exclusion AND -R80
    refund subtraction = -R160 total, instead of the correct 0).
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),        # allocation-based cash sum (none — legacy orders, no allocation rows)
        FakeResult(items=[8000]),    # order B's original sale still counted (payment_status filter now includes REFUNDED)
        FakeResult(items=[8000]),    # order B's refund_cash_cents, scoped by refunded_at
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=10000),
        user=user, db=db,
    )

    # expected = 10000 (float) + 8000 (sale, still counted) - 8000 (refund) = 10000 -> net zero effect
    assert result.expected_cash_cents == 10000
    assert result.variance_cents == 0


async def test_close_till_two_cash_sales_one_refunded_nets_correctly():
    """
    Order A (R80 cash, never refunded) + Order B (R80 cash, refunded in
    full) in the same session -> expected = float + both sales - B's
    refund = float + 8000 (A's net contribution), matching real physical
    drawer counting.
    """
    user = make_user(role="OWNER")
    session = make_session(user.business_id, opening_float_cents=10000)
    db = FakeDB([
        FakeResult(scalar=session),
        FakeResult(scalar=0),            # allocation-based cash sum (none — legacy orders, no allocation rows)
        FakeResult(items=[8000, 8000]),  # A (kept) + B (later refunded) — both counted as sales
        FakeResult(items=[8000]),        # B's refund
    ])

    result = await close_till(
        session_id=session.id,
        body=TillCloseRequest(counted_cash_cents=18000),
        user=user, db=db,
    )

    # expected = 10000 + 16000 (A+B sales) - 8000 (B refund) = 18000
    assert result.expected_cash_cents == 18000
    assert result.variance_cents == 0
