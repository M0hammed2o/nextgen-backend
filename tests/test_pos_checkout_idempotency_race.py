"""
POS checkout idempotency race (Wave 0A).

The early idempotency-key lookup in `create_pos_order` (a plain SELECT
before any write) closes the common "customer double-tapped, second
request arrives after the first already committed" case — but two
requests that arrive close enough together can BOTH pass that lookup
before either has committed. `uq_orders_business_idempotency_key`
(migration 0023) is what actually decides the winner; before this fix, the
loser's `db.flush()`/`db.commit()` raised an uncaught IntegrityError -> an
unhandled 500, even though only one order was ever actually created.

These are mocked unit tests proving the catch/replay logic in isolation.
The genuine two-connections-racing-against-a-real-Postgres proof lives in
tests/integration/test_wave0a_pos_concurrency.py.
"""
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1 import routes_pos
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest, POSOrderItemRequest, _is_unique_violation, create_pos_order,
)
from backend.app.core.rbac import AuthUser
from backend.app.pos.pricing import ResolvedPOSItem
from shared.models.till import TillSession
from shared.pricing.engine import LineItemBreakdown


class FakeResult:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


class FakeDB:
    """
    Like test_pos_orders.py's FakeDB, but `flush()` can be made to raise
    once — simulating the moment Postgres actually enforces the partial
    unique index on (business_id, idempotency_key), which happens at INSERT
    time (i.e. at this ORM's `db.flush()` right after `db.add(order)`), not
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
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now

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


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_till(business_id, status="OPEN"):
    return TillSession(
        id=uuid.uuid4(), business_id=business_id, opened_by_user_id=uuid.uuid4(),
        opening_float_cents=0, status=status, opened_at=datetime.now(timezone.utc),
    )


def make_business(**overrides):
    defaults = dict(
        delivery_fee_cents=0, max_staff_discount_cents=0,
        pos_cash_enabled=True, pos_card_enabled=True,
        pos_payfast_enabled=False, pos_yoco_enabled=False, pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False, pos_other_payment_enabled=False,
        pos_table_number_required=False,
        vat_registered=False, vat_rate_basis_points=0, vat_inclusive_pricing=True,
        inventory_enabled=False, timezone="Africa/Johannesburg",
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_resolved_item(line_total_cents=5000, requires_preparation=True):
    breakdown = LineItemBreakdown(
        base_price_cents=line_total_cents, option_adjustment_cents=0, add_on_total_cents=0,
        unit_price_cents=line_total_cents, quantity=1, line_total_cents=line_total_cents,
    )
    return ResolvedPOSItem(
        menu_item_id=uuid.uuid4(), name="Cheeseburger", breakdown=breakdown,
        selected_options_snapshot=[], add_ons_snapshot=[], removed_ingredients_snapshot=[],
        special_instructions=None, requires_preparation=requires_preparation,
    )


def make_body(key="cashier-double-tap"):
    return POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
        payment_method="CASH", cash_tendered_cents=5000, idempotency_key=key,
    )


def make_integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError("INSERT INTO orders ...", {}, Exception(
        f'duplicate key value violates unique constraint "{constraint_name}"'
    ))


# ── _is_unique_violation helper ──────────────────────────────────────────────

def test_is_unique_violation_matches_named_constraint():
    exc = make_integrity_error("uq_orders_business_idempotency_key")
    assert _is_unique_violation(exc, "uq_orders_business_idempotency_key") is True


def test_is_unique_violation_does_not_match_other_constraints():
    exc = make_integrity_error("some_other_constraint")
    assert _is_unique_violation(exc, "uq_orders_business_idempotency_key") is False


# ── create_pos_order: losing side of the race replays the winner ───────────

async def test_concurrent_checkout_loser_replays_winners_order_instead_of_500():
    user = make_user()
    body = make_body(key="cashier-double-tap")
    business = make_business()
    till = make_till(user.business_id)

    now = datetime.now(timezone.utc)
    winner = types.SimpleNamespace(
        id=uuid.uuid4(), business_id=user.business_id, idempotency_key=body.idempotency_key,
        order_number="BO-000042", status="ACCEPTED", payment_status="PAID",
        payment_required=False, payment_method="CASH", payment_reference=None,
        payment_link_url=None, paid_at=now, order_mode="PICKUP", table_number=None,
        source="POS", cash_tendered_cents=5000, change_due_cents=0,
        till_session_id=till.id, created_by_user_id=user.user_id,
        subtotal_cents=5000, delivery_fee_cents=0, discount_cents=0, tax_cents=0,
        discount_reason=None, total_cents=5000, currency="ZAR",
        refund_reference=None, refund_amount_cents=None, refunded_at=None,
        customer_name=None, phone_number=None, delivery_address=None,
        estimated_ready_at=None, confirmed_at=now, accepted_at=now, ready_at=None,
        completed_at=None, special_instructions=None, cancelled_reason=None,
        items=[], created_at=now, updated_at=now,
    )

    exc = make_integrity_error("uq_orders_business_idempotency_key")
    results = [
        FakeResult(scalar=None),          # early idempotency lookup: not found yet
        FakeResult(scalar=till),          # _get_open_till
        FakeResult(scalar=business),      # business lookup
        FakeResult(row=(1, "ZAR")),       # atomic order-number increment
        FakeResult(scalar=winner),        # _replay_idempotent_checkout's lookup after the race
    ]
    db = FakeDB(results, flush_exc=exc)

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
        order = await create_pos_order(body=body, user=user, db=db)

    assert order.id == winner.id
    assert order.order_number == "BO-000042"
    assert db.rollbacks == 1
    # The loser's own transaction never committed — only the (simulated,
    # already-committed-elsewhere) winner's order was returned.
    assert db.commits == 0


async def test_unrelated_integrity_error_during_checkout_is_not_swallowed():
    """A constraint violation that is NOT the idempotency-key index must still 500 (propagate), not be silently replayed as a stale/wrong order."""
    user = make_user()
    body = make_body(key="some-key")
    business = make_business()
    till = make_till(user.business_id)

    exc = make_integrity_error("some_unrelated_constraint")
    results = [
        FakeResult(scalar=None),
        FakeResult(scalar=till),
        FakeResult(scalar=business),
        FakeResult(row=(1, "ZAR")),
    ]
    db = FakeDB(results, flush_exc=exc)

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
        with pytest.raises(IntegrityError):
            await create_pos_order(body=body, user=user, db=db)
    assert db.rollbacks == 1


async def test_idempotency_race_without_a_key_is_impossible_to_trigger():
    """
    No idempotency_key at all -> the unique index's NULL-excluding partial
    condition means this specific race can't happen; an IntegrityError here
    would be a genuinely different bug and must propagate, not be masked.
    """
    user = make_user()
    body = make_body(key=None)
    business = make_business()
    till = make_till(user.business_id)

    exc = make_integrity_error("uq_orders_business_idempotency_key")
    results = [
        FakeResult(scalar=till),
        FakeResult(scalar=business),
        FakeResult(row=(1, "ZAR")),
    ]
    db = FakeDB(results, flush_exc=exc)

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
        with pytest.raises(IntegrityError):
            await create_pos_order(body=body, user=user, db=db)
