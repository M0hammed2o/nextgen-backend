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
