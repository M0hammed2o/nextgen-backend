"""
POS order creation / checkout route (backend/app/api/v1/routes_pos.py::create_pos_order).

Menu/option/add-on resolution itself is covered in test_pos_pricing.py; these
tests patch resolve_pos_items out and focus on what create_pos_order alone
owns: the till-open gate, payment-method-enabled gate, cash tender/change
computation, idempotent-replay, requires_preparation routing (kitchen vs
grab-and-go), the discount-limit gate, and the business-scope audit trail.

Restaurant POS redesign (2026-07-28): a POS order is now created already
PAID, with its fulfilment status resolved from whether any resolved item
needs kitchen preparation — this is the fix for BUG-0035 (POS orders used
to be forced through the same NEW->ACCEPTED->...->payment-prompt lifecycle
as a WhatsApp order).
"""
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_pos
from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.pos.pricing import ResolvedPOSItem
from shared.enums import OrderSource, OrderStatus
from shared.models.till import TillSession
from shared.pricing.engine import LineItemBreakdown


# ── Fakes ────────────────────────────────────────────────────────────────────

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
        # Simulate the id/created_at/updated_at defaults SQLAlchemy would
        # normally apply at INSERT time, so response schemas can validate.
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
        pos_yoco_enabled=False,
        pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False,
        pos_other_payment_enabled=False,
        pos_table_number_required=False,
        # VAT / receipt / inventory — added with the Ciao Pizza P0 work.
        # Defaults mirror the real Business model's column defaults so these
        # fakes keep matching the schema they stand in for.
        vat_registered=False,
        vat_number=None,
        vat_rate_basis_points=0,
        vat_inclusive_pricing=True,
        receipt_paper_width_mm=80,
        receipt_auto_print=True,
        receipt_copies=1,
        print_kitchen_receipt=False,
        receipt_config_json=None,
        inventory_enabled=False,
        wastage_approval_threshold_cents=0,
        # Loyalty entitlement (migration 0051, KNOWN_BUGS.md BUG-0107).
        # Defaults True here since most POS tests aren't about the
        # entitlement gate — the gate itself only ever reads this attribute
        # when body.loyalty_reward_id is set (short-circuited otherwise), so
        # this default doesn't mask the gate; see the dedicated
        # TestLoyaltyEntitlementGate tests below for loyalty_enabled=False.
        loyalty_enabled=True,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_resolved_item(line_total_cents=5000, requires_preparation=True):
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
        name="Cheeseburger",
        breakdown=breakdown,
        selected_options_snapshot=[],
        add_ons_snapshot=[],
        removed_ingredients_snapshot=[],
        special_instructions=None,
        requires_preparation=requires_preparation,
    )


def make_request(discount_cents=0, payment_method="CASH", cash_tendered_cents=5000,
                  idempotency_key=None, order_mode="PICKUP", table_number=None):
    kwargs = dict(
        items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
        discount_cents=discount_cents,
        payment_method=payment_method,
        order_mode=order_mode,
        table_number=table_number,
        idempotency_key=idempotency_key,
    )
    if payment_method == "CASH":
        kwargs["cash_tendered_cents"] = cash_tendered_cents
    return POSOrderCreateRequest(**kwargs)


async def _run(body, user, business=None, till=None, resolved_items=None, extra_results=()):
    """
    Runs create_pos_order with resolve_pos_items patched out. Builds the
    FakeDB results queue in the exact order create_pos_order issues
    db.execute() calls: [idempotency lookup?], till lookup, business lookup,
    [loyalty program lookup?], atomic order-number increment, *extra_results.
    """
    resolved_items = resolved_items if resolved_items is not None else [make_resolved_item()]
    business = business or make_business()
    till = till if till is not None else make_till(user.business_id)

    results = []
    if body.idempotency_key:
        results.append(FakeResult(scalar=None))  # no existing order with this key
    results.append(FakeResult(scalar=till))
    results.append(FakeResult(scalar=business))
    results.append(FakeResult(row=(1, "ZAR")))  # order_number_sequence, currency
    results.extend(extra_results)

    db = FakeDB(results)
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            order = await create_pos_order(body=body, user=user, db=db)
    return order, db


# ── Till gate ────────────────────────────────────────────────────────────────

async def test_checkout_blocked_when_till_not_open():
    user = make_user()
    body = make_request()
    db = FakeDB([FakeResult(scalar=None)])  # no open till session

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(side_effect=AssertionError("must not resolve items when till is closed"))):
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=user, db=db)
    assert exc.value.code == "TILL_NOT_OPEN"
    assert exc.value.status_code == 409


# ── Payment-method-enabled gate ─────────────────────────────────────────────

async def test_cash_only_business_rejects_card():
    user = make_user()
    body = make_request(payment_method="CARD")
    business = make_business(pos_cash_enabled=True, pos_card_enabled=False)

    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "PAYMENT_METHOD_NOT_ACCEPTED"


async def test_card_only_business_rejects_cash():
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=5000)
    business = make_business(pos_cash_enabled=False, pos_card_enabled=True)

    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "PAYMENT_METHOD_NOT_ACCEPTED"


async def test_cash_and_card_business_accepts_both():
    user = make_user()
    business = make_business(pos_cash_enabled=True, pos_card_enabled=True)

    cash_order, _ = await _run(make_request(payment_method="CASH", cash_tendered_cents=5000), user, business=business)
    assert cash_order.payment_method == "CASH"

    card_order, _ = await _run(make_request(payment_method="CARD"), user, business=business)
    assert card_order.payment_method == "CARD"


async def test_other_payment_enabled_when_any_provider_flag_is_on():
    user = make_user()
    business = make_business(pos_cash_enabled=False, pos_card_enabled=False, pos_yoco_enabled=True)
    order, _ = await _run(make_request(payment_method="OTHER"), user, business=business)
    assert order.payment_method == "OTHER"


async def test_other_payment_rejected_when_no_provider_flag_is_on():
    user = make_user()
    business = make_business(pos_cash_enabled=True, pos_card_enabled=True)  # no "other" flags on
    with pytest.raises(AppError) as exc:
        await _run(make_request(payment_method="OTHER"), user, business=business)
    assert exc.value.code == "PAYMENT_METHOD_NOT_ACCEPTED"


# ── Cash tender / change ─────────────────────────────────────────────────────

async def test_cash_change_computed_correctly():
    """R45 total (line_total 4500), R200 tendered -> R155 change."""
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=20000)
    order, _ = await _run(body, user, resolved_items=[make_resolved_item(line_total_cents=4500)])

    assert order.total_cents == 4500
    assert order.cash_tendered_cents == 20000
    assert order.change_due_cents == 15500


async def test_exact_cash_produces_zero_change():
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=5000)
    order, _ = await _run(body, user, resolved_items=[make_resolved_item(line_total_cents=5000)])
    assert order.change_due_cents == 0


async def test_cash_below_total_is_rejected():
    user = make_user()
    body = make_request(payment_method="CASH", cash_tendered_cents=1000)
    with pytest.raises(AppError) as exc:
        await _run(body, user, resolved_items=[make_resolved_item(line_total_cents=5000)])
    assert exc.value.code == "INSUFFICIENT_CASH_TENDERED"


async def test_negative_cash_tendered_rejected_by_schema():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CASH",
            cash_tendered_cents=-100,
        )


async def test_card_payment_rejects_cash_tendered_field():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CARD",
            cash_tendered_cents=1000,
        )


async def test_card_payment_never_sets_change_due():
    user = make_user()
    order, _ = await _run(make_request(payment_method="CARD"), user)
    assert order.cash_tendered_cents is None
    assert order.change_due_cents is None


# ── Idempotency ──────────────────────────────────────────────────────────────

async def test_duplicate_checkout_with_same_idempotency_key_returns_existing_order():
    user = make_user()
    key = "cashier-tap-12345"
    body = make_request(idempotency_key=key)

    existing_order = types.SimpleNamespace(
        id=uuid.uuid4(), business_id=user.business_id, idempotency_key=key,
        order_number="BO-000099", status="ACCEPTED", payment_status="PAID",
        payment_required=False, payment_method="CASH", payment_reference=None,
        payment_link_url=None, paid_at=datetime.now(timezone.utc),
        order_mode="PICKUP", table_number=None, source="POS",
        cash_tendered_cents=5000, change_due_cents=0, till_session_id=uuid.uuid4(),
        created_by_user_id=user.user_id, subtotal_cents=5000, delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, discount_reason=None, total_cents=5000,
        currency="ZAR", refund_reference=None, refund_amount_cents=None, refunded_at=None,
        customer_name=None, phone_number=None, delivery_address=None,
        estimated_ready_at=None, confirmed_at=datetime.now(timezone.utc),
        accepted_at=datetime.now(timezone.utc), ready_at=None, completed_at=None,
        special_instructions=None, cancelled_reason=None, items=[],
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db = FakeDB([FakeResult(scalar=existing_order)])  # idempotency lookup finds it immediately

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(side_effect=AssertionError("must not resolve items on replay"))):
        order = await create_pos_order(body=body, user=user, db=db)

    assert order.id == existing_order.id
    assert order.order_number == "BO-000099"
    assert db.commits == 0  # no new write happened — pure replay


async def test_different_idempotency_key_creates_a_new_order():
    user = make_user()
    order, db = await _run(make_request(idempotency_key="a-fresh-key"), user)
    assert order.source == OrderSource.POS.value
    assert db.commits == 1


# ── requires_preparation routing (kitchen vs grab-and-go) ───────────────────

async def test_order_needing_prep_starts_at_accepted_not_new():
    user = make_user()
    order, _ = await _run(make_request(), user, resolved_items=[make_resolved_item(requires_preparation=True)])
    assert order.status == OrderStatus.ACCEPTED.value
    assert order.payment_status == "PAID"


async def test_grab_and_go_only_sale_completes_immediately():
    user = make_user()
    order, _ = await _run(make_request(), user, resolved_items=[make_resolved_item(requires_preparation=False)])
    assert order.status == OrderStatus.COMPLETED.value


async def test_mixed_cart_with_any_prep_item_goes_to_kitchen():
    user = make_user()
    items = [make_resolved_item(requires_preparation=False), make_resolved_item(requires_preparation=True)]
    body = make_request(cash_tendered_cents=10000)  # 2 x 5000 = 10000 total
    order, _ = await _run(body, user, resolved_items=items)
    assert order.status == OrderStatus.ACCEPTED.value


# ── Table number requirement ─────────────────────────────────────────────────

async def test_table_number_required_for_dine_in_when_configured():
    user = make_user()
    business = make_business(pos_table_number_required=True)
    body = make_request(order_mode="DINE_IN", table_number=None)

    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "TABLE_NUMBER_REQUIRED"


async def test_table_number_provided_for_dine_in_succeeds():
    user = make_user()
    business = make_business(pos_table_number_required=True)
    body = make_request(order_mode="DINE_IN", table_number="12")
    order, _ = await _run(body, user, business=business)
    assert order.table_number == "12"


# ── Discount limit enforcement (unchanged behaviour from Phase 1) ──────────

async def test_staff_discount_within_limit_succeeds():
    user = make_user(role="STAFF")
    body = make_request(discount_cents=500)
    business = make_business(max_staff_discount_cents=1000)
    order, db = await _run(body, user, business=business)

    assert order.source == OrderSource.POS.value
    assert db.commits == 1


async def test_staff_discount_above_limit_is_blocked():
    user = make_user(role="STAFF")
    body = make_request(discount_cents=1500)
    business = make_business(max_staff_discount_cents=1000)

    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"
    assert exc.value.status_code == 403


async def test_manager_discount_above_limit_is_also_blocked():
    """Only OWNER bypasses the limit — MANAGER is subject to it, same as STAFF."""
    user = make_user(role="MANAGER")
    body = make_request(discount_cents=1500)
    business = make_business(max_staff_discount_cents=1000)

    with pytest.raises(AppError) as exc:
        await _run(body, user, business=business)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"


async def test_owner_discount_above_limit_is_allowed():
    user = make_user(role="OWNER")
    body = make_request(discount_cents=1500)
    business = make_business(max_staff_discount_cents=1000)
    order, db = await _run(body, user, business=business)

    assert order.discount_cents == 1500
    assert db.commits == 1


async def test_discount_exceeding_subtotal_is_rejected_before_any_db_write():
    user = make_user(role="OWNER")
    body = make_request(discount_cents=9999)
    business = make_business()
    till = make_till(user.business_id)

    resolved_items = [make_resolved_item(line_total_cents=5000)]
    db = FakeDB([FakeResult(scalar=till), FakeResult(scalar=business)])
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=user, db=db)

    assert exc.value.code == "DISCOUNT_EXCEEDS_SUBTOTAL"
    assert db.added == []
    assert db.commits == 0


# ── Audit trail ──────────────────────────────────────────────────────────────

async def test_discount_writes_a_business_scope_audit_event():
    user = make_user(role="OWNER")
    body = make_request(discount_cents=500)
    order, db = await _run(body, user, business=make_business(max_staff_discount_cents=0))

    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].scope == "BUSINESS"
    assert audit_events[0].action == "order.pos.discount_applied"
    assert audit_events[0].diff_json["discount_cents"] == 500


async def test_no_discount_writes_no_audit_event():
    user = make_user(role="STAFF")
    body = make_request(discount_cents=0)
    order, db = await _run(body, user)

    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert audit_events == []


# ── Cashier / till linkage ───────────────────────────────────────────────────

async def test_order_records_cashier_and_till_session():
    user = make_user(role="STAFF")
    till = make_till(user.business_id)
    order, _ = await _run(make_request(), user, till=till)

    assert order.created_by_user_id == user.user_id
    assert order.till_session_id == till.id


# ── Loyalty redemption at checkout ──────────────────────────────────────────
# Schema-level guards (customer_phone / idempotency_key required alongside
# loyalty_reward_id) live in POSOrderCreateRequest's own validator — tested
# directly against the schema, no route/DB involvement needed.

def test_loyalty_reward_requires_customer_phone():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CARD",
            loyalty_reward_id=uuid.uuid4(),
            idempotency_key="key-1",
        )


def test_loyalty_reward_requires_idempotency_key():
    with pytest.raises(ValueError):
        POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CARD",
            loyalty_reward_id=uuid.uuid4(),
            customer_phone="0821234567",
        )


def make_program(**overrides):
    defaults = dict(is_enabled=True, min_redemption_points=0, silver_tier_points=500, gold_tier_points=2000)
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_loyalty_customer(business_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(), business_id=business_id, loyalty_points_balance=500,
        loyalty_tier="BRONZE", wa_id=None, phone_number="+27821234567", display_name=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def make_reward(business_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(), business_id=business_id, name="Free Coffee",
        points_cost=200, is_active=True, value_cents=3000,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


async def _run_with_loyalty(body, user, *, program=None, customer=None, reward=None,
                             business=None, till=None, resolved_items=None):
    """
    Builds the FakeDB results queue for the loyalty-redemption path:
    [idempotency lookup], till, business, program lookup, customer lookup
    (get_or_create_customer_by_phone), [reward lookup, customer FOR UPDATE
    lock (redeem_reward)], order-number increment.
    """
    resolved_items = resolved_items if resolved_items is not None else [make_resolved_item()]
    business = business or make_business()
    till = till if till is not None else make_till(user.business_id)

    results = []
    if body.idempotency_key:
        results.append(FakeResult(scalar=None))
    results.append(FakeResult(scalar=till))
    results.append(FakeResult(scalar=business))
    results.append(FakeResult(scalar=program))  # program lookup
    if program and program.is_enabled:
        results.append(FakeResult(scalar=customer))  # get_or_create_customer_by_phone lookup
        if body.loyalty_reward_id is not None:
            results.append(FakeResult(scalar=reward))  # reward lookup
            if reward is not None:
                results.append(FakeResult(scalar=customer))  # redeem_reward's FOR UPDATE lock
    results.append(FakeResult(row=(1, "ZAR")))  # order-number increment

    db = FakeDB(results)
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            order = await create_pos_order(body=body, user=user, db=db)
    return order, db


async def test_loyalty_redemption_applies_discount_and_deducts_points():
    user = make_user(role="STAFF")
    program = make_program()
    customer = make_loyalty_customer(user.business_id, loyalty_points_balance=500)
    reward = make_reward(user.business_id, points_cost=200, value_cents=3000)
    body = make_request(
        payment_method="CASH", cash_tendered_cents=2000,
        idempotency_key="redeem-1",
    )
    body = body.model_copy(update={"loyalty_reward_id": reward.id, "customer_phone": "0821234567"})

    order, db = await _run_with_loyalty(
        body, user, program=program, customer=customer, reward=reward,
        resolved_items=[make_resolved_item(line_total_cents=5000)],
    )

    assert order.loyalty_discount_cents == 3000
    assert order.loyalty_points_redeemed == 200
    assert order.loyalty_reward_id == reward.id
    assert order.loyalty_reward_name == "Free Coffee"
    assert order.total_cents == 2000  # 5000 subtotal - 3000 loyalty discount
    assert order.change_due_cents == 0
    assert customer.loyalty_points_balance == 300  # 500 - 200
    assert db.commits == 1


async def test_loyalty_redemption_clamps_when_reward_exceeds_sale_and_audits_shortfall():
    user = make_user(role="STAFF")
    program = make_program()
    customer = make_loyalty_customer(user.business_id, loyalty_points_balance=500)
    reward = make_reward(user.business_id, points_cost=200, value_cents=9000)  # R90 reward
    body = make_request(payment_method="CARD", idempotency_key="redeem-2")
    body = body.model_copy(update={"loyalty_reward_id": reward.id, "customer_phone": "0821234567"})

    order, db = await _run_with_loyalty(
        body, user, program=program, customer=customer, reward=reward,
        resolved_items=[make_resolved_item(line_total_cents=5000)],  # R50 sale
    )

    assert order.loyalty_discount_cents == 5000  # clamped to the subtotal
    assert order.loyalty_points_redeemed == 200  # full points cost regardless
    assert order.total_cents == 0
    assert customer.loyalty_points_balance == 300

    from shared.models.audit import AuditEvent
    loyalty_events = [o for o in db.added if isinstance(o, AuditEvent) and o.action == "order.pos.loyalty_redeemed"]
    assert len(loyalty_events) == 1
    assert loyalty_events[0].diff_json["shortfall_cents"] == 4000  # 9000 - 5000
    assert loyalty_events[0].diff_json["points_redeemed"] == 200


async def test_loyalty_redemption_rejected_when_program_disabled():
    user = make_user(role="STAFF")
    program = make_program(is_enabled=False)
    reward_id = uuid.uuid4()
    body = make_request(payment_method="CARD", idempotency_key="redeem-3")
    body = body.model_copy(update={"loyalty_reward_id": reward_id, "customer_phone": "0821234567"})

    with pytest.raises(AppError) as exc:
        await _run_with_loyalty(body, user, program=program, customer=None, reward=None)
    assert exc.value.code == "LOYALTY_DISABLED"


# ── Business-level entitlement gate (migration 0051, KNOWN_BUGS.md BUG-0107) ─

async def test_pos_loyalty_redemption_refused_for_non_entitled_business_nothing_mutated():
    """
    A request carrying loyalty_reward_id for a business without
    Business.loyalty_enabled must be refused BEFORE any mutation — checked
    immediately after the business is loaded, ahead of payment-method
    validation, item resolution, customer creation, the order-number
    sequence increment, and the redemption itself. Proven here by giving
    FakeDB only [idempotency lookup, till, business] — if the route reached
    any further query (item resolution is patched to assert-fail; a 4th
    execute() would IndexError on an empty queue), this test would fail for
    a different reason than the assertion below.
    """
    user = make_user(role="STAFF")
    business = make_business(loyalty_enabled=False)
    till = make_till(user.business_id)
    body = make_request(payment_method="CARD", idempotency_key="redeem-gate-1")
    body = body.model_copy(update={"loyalty_reward_id": uuid.uuid4(), "customer_phone": "0821234567"})

    db = FakeDB([
        FakeResult(scalar=None),  # idempotency lookup: no existing order
        FakeResult(scalar=till),
        FakeResult(scalar=business),
    ])
    with patch.object(
        routes_pos, "resolve_pos_items",
        AsyncMock(side_effect=AssertionError("must not resolve items for a non-entitled business")),
    ):
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=user, db=db)
    assert exc.value.code == "LOYALTY_NOT_ENABLED"
    assert exc.value.status_code == 409
    assert db.commits == 0
    assert db.added == []


async def test_pos_checkout_without_reward_unaffected_by_non_entitled_business():
    """The entitlement gate is scoped to loyalty_reward_id only — a normal
    (non-loyalty) checkout must succeed regardless of Business.loyalty_enabled,
    and a customer_phone-only checkout (points EARNING) is not gated either,
    per this file's/routes_loyalty.py's deliberate earning carve-out."""
    user = make_user(role="STAFF")
    business = make_business(loyalty_enabled=False)
    body = make_request(payment_method="CARD")

    order, db = await _run(body, user, business=business)

    assert order is not None
    assert db.commits == 1


async def test_loyalty_redemption_rejects_reward_with_no_cents_value():
    """A reward with no value_cents is manual-handover-only at the till."""
    user = make_user(role="STAFF")
    program = make_program()
    customer = make_loyalty_customer(user.business_id)
    reward = make_reward(user.business_id, value_cents=None)
    body = make_request(payment_method="CARD", idempotency_key="redeem-4")
    body = body.model_copy(update={"loyalty_reward_id": reward.id, "customer_phone": "0821234567"})

    with pytest.raises(AppError) as exc:
        await _run_with_loyalty(body, user, program=program, customer=customer, reward=reward)
    assert exc.value.code == "REWARD_NOT_REDEEMABLE_AT_TILL"


async def test_loyalty_redemption_rejects_inactive_reward():
    """Reward deactivated between selection and checkout — 422, nothing moved."""
    user = make_user(role="STAFF")
    program = make_program()
    customer = make_loyalty_customer(user.business_id, loyalty_points_balance=500)
    reward = make_reward(user.business_id, is_active=False)
    body = make_request(payment_method="CARD", idempotency_key="redeem-5")
    body = body.model_copy(update={"loyalty_reward_id": reward.id, "customer_phone": "0821234567"})

    with pytest.raises(AppError) as exc:
        await _run_with_loyalty(body, user, program=program, customer=customer, reward=reward)
    assert exc.value.code == "REWARD_INACTIVE"
    assert exc.value.status_code == 422
    assert customer.loyalty_points_balance == 500  # untouched


async def test_loyalty_redemption_excluded_from_staff_discount_limit_check():
    """
    A reward's discount must never be routed through the max_staff_discount_cents
    gate — that check only ever looks at body.discount_cents (the staff-typed
    manual discount), which stays 0 here.
    """
    user = make_user(role="STAFF")
    business = make_business(max_staff_discount_cents=0)  # zero staff discretion
    program = make_program()
    customer = make_loyalty_customer(user.business_id, loyalty_points_balance=500)
    reward = make_reward(user.business_id, points_cost=200, value_cents=3000)
    body = make_request(payment_method="CARD", idempotency_key="redeem-6")
    body = body.model_copy(update={"loyalty_reward_id": reward.id, "customer_phone": "0821234567"})

    order, db = await _run_with_loyalty(
        body, user, program=program, customer=customer, reward=reward, business=business,
        resolved_items=[make_resolved_item(line_total_cents=5000)],
    )
    assert order.loyalty_discount_cents == 3000
    assert db.commits == 1


async def test_invalid_customer_phone_never_burns_an_order_number():
    """
    BUG-0102: the customer/loyalty resolution block used to run AFTER the
    order-number sequence increment — a phone-validation failure there
    burned an order number permanently. It was moved earlier specifically
    to close this; this test proves it by giving FakeDB exactly [till,
    business, program] and nothing else. If the route still reached the
    order-number increment (or even the customer SELECT) before raising,
    popping a 4th result from an empty queue would raise IndexError instead
    of the expected AppError.
    """
    user = make_user(role="STAFF")
    program = make_program()  # is_enabled=True
    business = make_business()
    till = make_till(user.business_id)
    body = make_request(payment_method="CARD")
    body = body.model_copy(update={"customer_phone": "not-a-real-phone-number"})

    db = FakeDB([
        FakeResult(scalar=till),
        FakeResult(scalar=business),
        FakeResult(scalar=program),
    ])
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=user, db=db)
    assert exc.value.code == "INVALID_PHONE_NUMBER"
    assert db.commits == 0


async def test_loyalty_redemption_double_tap_same_idempotency_key_replays():
    """A double-tapped redeem-checkout must replay the winning order, never redeem twice."""
    user = make_user(role="STAFF")
    key = "redeem-double-tap"
    body = POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
        payment_method="CARD",
        loyalty_reward_id=uuid.uuid4(),
        customer_phone="0821234567",
        idempotency_key=key,
    )
    existing_order = types.SimpleNamespace(
        id=uuid.uuid4(), business_id=user.business_id, idempotency_key=key,
        order_number="BO-000042", status="ACCEPTED", payment_status="PAID",
        payment_required=False, payment_method="CARD", payment_reference=None,
        payment_link_url=None, paid_at=datetime.now(timezone.utc),
        order_mode="PICKUP", table_number=None, source="POS",
        cash_tendered_cents=None, change_due_cents=None, till_session_id=uuid.uuid4(),
        created_by_user_id=user.user_id, subtotal_cents=5000, delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, discount_reason=None,
        loyalty_discount_cents=3000, loyalty_reward_id=uuid.uuid4(),
        loyalty_points_redeemed=200, loyalty_reward_name="Free Coffee",
        total_cents=2000, currency="ZAR", refund_reference=None, refund_amount_cents=None,
        refunded_at=None, customer_name=None, phone_number="+27821234567",
        delivery_address=None, estimated_ready_at=None, confirmed_at=datetime.now(timezone.utc),
        accepted_at=datetime.now(timezone.utc), ready_at=None, completed_at=None,
        special_instructions=None, cancelled_reason=None, items=[],
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db = FakeDB([FakeResult(scalar=existing_order)])  # idempotency lookup finds it immediately

    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(side_effect=AssertionError("must not resolve items on replay"))):
        order = await create_pos_order(body=body, user=user, db=db)

    assert order.id == existing_order.id
    assert order.loyalty_discount_cents == 3000
    assert order.loyalty_points_redeemed == 200
    assert db.commits == 0  # pure replay, no second redemption
