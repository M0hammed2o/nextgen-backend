"""
Split payments + WhatsApp Pay by Link — checkout-time validation and
allocation-row creation (backend/app/api/v1/routes_pos.py::create_pos_order).

Till cash-up and refund/void cash-portion logic are covered in
tests/test_pos_till.py and tests/test_pos_refund_void.py respectively; the
payment-intent CRUD endpoints are covered in
tests/test_pos_payment_intents.py. See docs/SPLIT_PAYMENTS_CONTRACT.md for
the full design this pins down.
"""
import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_pos
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSPaymentAllocationRequest,
    create_pos_order,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.pos_payment import POSPaymentAllocation, POSPaymentLinkIntent
from shared.models.till import TillSession
from shared.pricing.engine import LineItemBreakdown
from backend.app.pos.pricing import ResolvedPOSItem


# ── Fakes (same conventions as tests/test_pos_orders.py) ─────────────────────

class FakeResult:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


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
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

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
        delivery_fee_cents=0,
        max_staff_discount_cents=0,
        pos_cash_enabled=True,
        pos_card_enabled=True,
        pos_payfast_enabled=False,
        pos_yoco_enabled=True,
        pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False,
        pos_other_payment_enabled=False,
        pos_table_number_required=False,
        vat_registered=False,
        vat_rate_basis_points=0,
        vat_inclusive_pricing=True,
        inventory_enabled=False,
        loyalty_enabled=True,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_resolved_item(line_total_cents=30000, requires_preparation=False):
    breakdown = LineItemBreakdown(
        base_price_cents=line_total_cents,
        option_adjustment_cents=0,
        add_on_total_cents=0,
        unit_price_cents=line_total_cents,
        quantity=1,
        line_total_cents=line_total_cents,
    )
    return ResolvedPOSItem(
        menu_item_id=uuid.uuid4(),
        name="Family Platter",
        breakdown=breakdown,
        selected_options_snapshot=[],
        add_ons_snapshot=[],
        removed_ingredients_snapshot=[],
        special_instructions=None,
        requires_preparation=requires_preparation,
    )


def make_intent(business_id, *, amount_cents=20000, status="PAID", provider="YOCO", order_id=None):
    now = datetime.now(timezone.utc)
    return POSPaymentLinkIntent(
        id=uuid.uuid4(),
        business_id=business_id,
        amount_cents=amount_cents,
        currency="ZAR",
        provider=provider,
        status=status,
        provider_reference="chr_test123",
        customer_phone="+27821234567",
        whatsapp_status="SENT",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        paid_at=now if status in ("PAID", "CONSUMED") else None,
        order_id=order_id,
    )


def make_request(payments=None, payment_method=None, cash_tendered_cents=None, idempotency_key=None):
    kwargs = dict(
        items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
        order_mode="PICKUP",
        idempotency_key=idempotency_key,
    )
    if payments is not None:
        kwargs["payments"] = payments
    else:
        kwargs["payment_method"] = payment_method or "CASH"
        if (payment_method or "CASH") == "CASH":
            kwargs["cash_tendered_cents"] = cash_tendered_cents or 30000
    return POSOrderCreateRequest(**kwargs)


async def _run(body, user, business=None, till=None, resolved_items=None, extra_results=()):
    """
    Builds the FakeDB results queue in the exact order create_pos_order
    issues db.execute() calls: [idempotency lookup?], till lookup, business
    lookup, *extra_results (e.g. one per PAY_BY_LINK allocation's intent
    lock), order-number increment.
    """
    resolved_items = resolved_items if resolved_items is not None else [make_resolved_item()]
    business = business or make_business()
    till = till if till is not None else make_till(user.business_id)

    results = []
    if body.idempotency_key:
        results.append(FakeResult(scalar=None))
    results.append(FakeResult(scalar=till))
    results.append(FakeResult(scalar=business))
    results.extend(extra_results)
    results.append(FakeResult(row=(1, "ZAR")))

    db = FakeDB(results)
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            order = await create_pos_order(body=body, user=user, db=db)
    return order, db


def allocations_of(db):
    return [o for o in db.added if isinstance(o, POSPaymentAllocation)]


def make_fake_order(user, **overrides):
    """A SimpleNamespace with every field OrderResponse.model_validate needs —
    same shape as tests/test_pos_orders.py's idempotent-replay fixtures."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), business_id=user.business_id, idempotency_key="key-abc",
        order_number="BO-000099", status="ACCEPTED", payment_status="PAID",
        payment_required=False, payment_method="SPLIT", payment_reference=None,
        payment_link_url=None, paid_at=now,
        order_mode="PICKUP", table_number=None, source="POS",
        cash_tendered_cents=None, change_due_cents=None, till_session_id=uuid.uuid4(),
        created_by_user_id=user.user_id, subtotal_cents=30000, delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, discount_reason=None, total_cents=30000,
        currency="ZAR", refund_reference=None, refund_amount_cents=None, refunded_at=None,
        customer_name=None, phone_number=None, delivery_address=None,
        estimated_ready_at=None, confirmed_at=now,
        accepted_at=now, ready_at=None, completed_at=None,
        special_instructions=None, cancelled_reason=None, items=[],
        created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


# ── Unified design: a legacy single-method sale still gets exactly 1 row ─────

async def test_legacy_cash_checkout_creates_exactly_one_allocation():
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=35000)
    order, db = await _run(body, user)

    assert order.payment_method == "CASH"
    allocs = allocations_of(db)
    assert len(allocs) == 1
    assert allocs[0].payment_method == "CASH"
    assert allocs[0].amount_cents == 30000
    assert allocs[0].cash_tendered_cents == 35000
    assert allocs[0].change_due_cents == 5000


async def test_legacy_checkout_fully_covered_by_loyalty_reward_creates_zero_allocations():
    """
    Regression (found via the real-Postgres integration suite): a loyalty
    reward whose value clamps the discount so total_cents == 0 must not
    try to insert a zero-amount allocation row — ck_pos_payment_allocations_
    amount_positive would reject it, and semantically no money changed
    hands at all, so there is nothing to record.
    """
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=0)
    resolved_items = [make_resolved_item(line_total_cents=0)]
    order, db = await _run(body, user, resolved_items=resolved_items)

    assert order.total_cents == 0
    assert allocations_of(db) == []


async def test_legacy_card_checkout_creates_exactly_one_allocation_no_cash_fields():
    user = make_user()
    body = make_request(payment_method="CARD")
    order, db = await _run(body, user)

    allocs = allocations_of(db)
    assert len(allocs) == 1
    assert allocs[0].payment_method == "CARD"
    assert allocs[0].cash_tendered_cents is None
    assert allocs[0].change_due_cents is None


# ── Split: Cash + Card (no Pay by Link involved) ──────────────────────────────

async def test_split_cash_and_card_creates_split_order_and_two_allocations():
    user = make_user()
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="CARD", amount_cents=20000, payment_reference="term-1"),
    ])
    order, db = await _run(body, user)

    assert order.payment_method == "SPLIT"
    assert order.payment_reference is None
    assert order.cash_tendered_cents is None
    assert order.change_due_cents is None

    allocs = allocations_of(db)
    assert len(allocs) == 2
    cash = next(a for a in allocs if a.payment_method == "CASH")
    card = next(a for a in allocs if a.payment_method == "CARD")
    assert cash.amount_cents == 10000
    assert cash.cash_tendered_cents == 10000
    assert cash.change_due_cents == 0
    assert card.amount_cents == 20000
    assert card.payment_reference == "term-1"


async def test_split_payment_total_mismatch_rejected():
    user = make_user()
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="CARD", amount_cents=10000),
    ])
    with pytest.raises(AppError) as exc:
        await _run(body, user)
    assert exc.value.code == "PAYMENT_TOTAL_MISMATCH"


async def test_split_payment_method_disabled_at_business_rejected():
    user = make_user()
    business = make_business(pos_cash_enabled=False)
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="CARD", amount_cents=20000),
    ])
    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "PAYMENT_METHOD_NOT_ACCEPTED"


async def test_payments_array_requires_at_least_two_entries():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payments=[POSPaymentAllocationRequest(payment_method="CASH", amount_cents=100, cash_tendered_cents=100)],
        )


async def test_payment_method_and_payments_are_mutually_exclusive():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CASH",
            cash_tendered_cents=100,
            payments=[
                POSPaymentAllocationRequest(payment_method="CASH", amount_cents=50, cash_tendered_cents=50),
                POSPaymentAllocationRequest(payment_method="CARD", amount_cents=50),
            ],
        )


# ── Split: Pay by Link ────────────────────────────────────────────────────────

async def test_split_pay_by_link_happy_path_consumes_intent_and_creates_allocation():
    user = make_user()
    intent = make_intent(user.business_id, amount_cents=20000, status="PAID")
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=intent.id),
    ])
    order, db = await _run(body, user, extra_results=[FakeResult(scalar=intent)])

    assert order.payment_method == "SPLIT"
    assert intent.status == "CONSUMED"
    assert intent.order_id == order.id
    assert intent.consumed_at is not None

    allocs = allocations_of(db)
    link_alloc = next(a for a in allocs if a.payment_method == "PAY_BY_LINK")
    assert link_alloc.amount_cents == 20000
    assert link_alloc.provider == "YOCO"
    assert link_alloc.payment_link_intent_id == intent.id
    assert link_alloc.payment_reference == intent.provider_reference


async def test_split_pay_by_link_intent_not_paid_rejected():
    user = make_user()
    intent = make_intent(user.business_id, amount_cents=20000, status="PENDING")
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=intent.id),
    ])
    with pytest.raises(AppError) as exc:
        await _run(body, user, extra_results=[FakeResult(scalar=intent)])
    assert exc.value.code == "PAYMENT_INTENT_NOT_PAID"


async def test_split_pay_by_link_amount_mismatch_rejected():
    user = make_user()
    # Intent confirmed for R150, but this checkout tries to allocate R200 against it.
    intent = make_intent(user.business_id, amount_cents=15000, status="PAID")
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=intent.id),
    ])
    with pytest.raises(AppError) as exc:
        await _run(body, user, extra_results=[FakeResult(scalar=intent)])
    assert exc.value.code == "PAYMENT_INTENT_AMOUNT_MISMATCH"


async def test_split_pay_by_link_wrong_tenant_intent_is_not_found():
    user = make_user()
    other_business_intent = make_intent(uuid.uuid4(), amount_cents=20000, status="PAID")
    body = make_request(payments=[
        POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
        POSPaymentAllocationRequest(
            payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=other_business_intent.id,
        ),
    ])
    # The tenant-scoped lookup query (business_id == user.business_id) would
    # never return a row for another tenant's intent — simulated here as None.
    with pytest.raises(NotFoundError):
        await _run(body, user, extra_results=[FakeResult(scalar=None)])


async def test_split_pay_by_link_already_consumed_by_different_checkout_rejected():
    user = make_user()
    other_order_id = uuid.uuid4()
    intent = make_intent(user.business_id, amount_cents=20000, status="CONSUMED", order_id=other_order_id)
    body = make_request(
        payments=[
            POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
            POSPaymentAllocationRequest(payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=intent.id),
        ],
        idempotency_key="key-abc",
    )
    # body has an idempotency_key, so the handler will look up the order
    # this intent was consumed into — simulate that it has a DIFFERENT key
    # (genuinely a different checkout trying to reuse a spent intent).
    different_order = make_fake_order(user, idempotency_key="some-other-key")
    with pytest.raises(AppError) as exc:
        await _run(
            body, user,
            extra_results=[FakeResult(scalar=intent), FakeResult(scalar=different_order)],
        )
    assert exc.value.code == "PAYMENT_INTENT_ALREADY_CONSUMED"


async def test_split_pay_by_link_already_consumed_same_idempotency_key_replays():
    user = make_user()
    original_order = make_fake_order(user, idempotency_key="key-abc")
    intent = make_intent(user.business_id, amount_cents=20000, status="CONSUMED", order_id=original_order.id)
    body = make_request(
        payments=[
            POSPaymentAllocationRequest(payment_method="CASH", amount_cents=10000, cash_tendered_cents=10000),
            POSPaymentAllocationRequest(payment_method="PAY_BY_LINK", amount_cents=20000, payment_intent_id=intent.id),
        ],
        idempotency_key="key-abc",
    )
    db = FakeDB([
        FakeResult(scalar=None),  # early idempotency lookup — not yet visible to THIS request
        FakeResult(scalar=make_till(user.business_id)),
        FakeResult(scalar=make_business()),
        FakeResult(scalar=intent),          # intent lock — already CONSUMED by the race winner
        FakeResult(scalar=original_order),  # same idempotency_key -> replay
    ])
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
        result = await create_pos_order(body=body, user=user, db=db)
    assert result.id == original_order.id
    # No new allocation/order was created on the replay path.
    assert allocations_of(db) == []
