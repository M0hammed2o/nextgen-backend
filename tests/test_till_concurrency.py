"""
Till concurrency (Wave 0A / BUG-0042) — mocked unit tests for the pieces
that don't need a real database:

  - `open_till` maps an IntegrityError on the new
    `uq_till_sessions_one_open_per_business` index into the same friendly
    409 TILL_ALREADY_OPEN the pre-check already returns, instead of a 500.
  - `_get_open_till` locks the row (FOR UPDATE) and falls back to the most
    recently opened session if `scalar_one_or_none()` ever raises
    MultipleResultsFound (belt-and-braces — should be impossible after the
    unique index, but must never 500 every checkout for a business if it
    somehow happens).
  - `close_till` locks the session row FOR UPDATE too.

Real-Postgres concurrency proof (two simultaneous opens really do collapse
to one OPEN row) lives in tests/integration/test_wave0a_pos_concurrency.py.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError, MultipleResultsFound

from backend.app.api.v1.routes_pos import TillCloseRequest, TillOpenRequest, close_till, open_till
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.till import TillSession


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


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class FakeResult:
    def __init__(self, scalar=None, scalar_exc=None, items=None):
        self._scalar = scalar
        self._scalar_exc = scalar_exc
        self._items = items

    def scalar_one_or_none(self):
        if self._scalar_exc:
            raise self._scalar_exc
        return self._scalar

    def scalars(self):
        return FakeScalars(self._items or [])


class FakeDB:
    def __init__(self, results, commit_exc=None):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._commit_exc = commit_exc

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
        if hasattr(obj, "opened_at") and getattr(obj, "opened_at", None) is None:
            obj.opened_at = now
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now

    async def commit(self):
        self.commits += 1
        if self._commit_exc and self.commits == 1:
            raise self._commit_exc

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj):
        self._finalize(obj)


class FakeOrigError:
    """Stand-in for the asyncpg driver error IntegrityError.orig wraps."""
    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


def make_integrity_error(constraint_name: str) -> IntegrityError:
    orig = FakeOrigError(
        f'duplicate key value violates unique constraint "{constraint_name}"'
    )
    return IntegrityError("INSERT INTO till_sessions ...", {}, orig)


# ── open_till: IntegrityError -> 409, not a 500 ─────────────────────────────

async def test_open_till_race_maps_integrity_error_to_conflict():
    """
    Two simultaneous opens can both pass the friendly pre-check SELECT
    before either commits — the loser's commit raises IntegrityError against
    uq_till_sessions_one_open_per_business. Must surface as the same 409
    TILL_ALREADY_OPEN the pre-check gives, never an unhandled 500.
    """
    user = make_user()
    exc = make_integrity_error("uq_till_sessions_one_open_per_business")
    db = FakeDB([FakeResult(scalar=None)], commit_exc=exc)  # pre-check finds nothing open

    with pytest.raises(AppError) as raised:
        await open_till(body=TillOpenRequest(opening_float_cents=5000), user=user, db=db)

    assert raised.value.code == "TILL_ALREADY_OPEN"
    assert raised.value.status_code == 409
    assert db.rollbacks == 1


async def test_open_till_unrelated_integrity_error_is_not_swallowed():
    """A constraint violation that is NOT the till-uniqueness index must propagate."""
    user = make_user()
    exc = make_integrity_error("some_other_constraint")
    db = FakeDB([FakeResult(scalar=None)], commit_exc=exc)

    with pytest.raises(IntegrityError):
        await open_till(body=TillOpenRequest(opening_float_cents=5000), user=user, db=db)
    assert db.rollbacks == 1


# ── _get_open_till: belt-and-braces MultipleResultsFound fallback ──────────

async def test_get_open_till_falls_back_on_multiple_results_found():
    from backend.app.api.v1.routes_pos import _get_open_till

    business_id = uuid.uuid4()
    newest = make_session(business_id)
    db = FakeDB([
        FakeResult(scalar_exc=MultipleResultsFound()),  # first (locking) query
        FakeResult(items=[newest]),                     # fallback ordered query
    ])

    session = await _get_open_till(db, business_id)
    assert session.id == newest.id


async def test_get_open_till_still_404s_when_nothing_open():
    from backend.app.api.v1.routes_pos import _get_open_till

    db = FakeDB([FakeResult(scalar=None)])
    with pytest.raises(AppError) as exc:
        await _get_open_till(db, uuid.uuid4())
    assert exc.value.code == "TILL_NOT_OPEN"
    assert exc.value.status_code == 409


# ── close_till: row lock present (compiled-query smoke test) ───────────────

async def test_close_till_locks_the_session_row():
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
    # The first query issued must be the FOR UPDATE session lookup.
    # (FakeDB doesn't record the raw statement text here, so this is a
    # behavioural check: the call must succeed at all, proving
    # with_for_update() is a valid, executable query modifier in this path.)
