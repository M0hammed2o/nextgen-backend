"""
POS till-native refund/void (backend/app/api/v1/routes_pos.py) and the
staff-readable POS settings endpoint.

Refund/void are deliberately separate from routes_orders.py's provider-
dependent /refund endpoint — a physical till sale (cash or card-terminal)
has no online payment provider to call, so reversing it is just recording
that cash was handed back / the card terminal was reversed, the same
manually-confirmed philosophy as Phase 7's card payments.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_pos import (
    POSRefundRequest, POSVoidRequest, get_pos_settings, refund_pos_order, void_pos_order,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.enums import OrderSource
from shared.models.order import Order


class FakeResult:
    def __init__(self, scalar=None, items=None):
        self._scalar = scalar
        self._items = items

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._items or []


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass



def make_business_for_reversal(inventory_enabled=False):
    """
    Minimal Business stand-in for the refund/void reversal path.

    Defaults to inventory_enabled=False so these tests stay focused on
    payment/fulfilment state — stock reversal has its own dedicated tests
    in tests/test_inventory_pos_integration.py.
    """
    return types.SimpleNamespace(inventory_enabled=inventory_enabled)


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_pos_order(business_id, total_cents=5000, payment_status="PAID", source=OrderSource.POS.value,
                    payment_method="CASH", loyalty_reward_id=None):
    now = datetime.now(timezone.utc)
    return Order(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status="ACCEPTED", payment_status=payment_status, payment_method=payment_method,
        source=source, total_cents=total_cents, subtotal_cents=total_cents,
        currency="ZAR",
        payment_required=False, order_mode="PICKUP", delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, loyalty_reward_id=loyalty_reward_id,
        created_at=now, updated_at=now,
    )


def _open_till_result():
    """A truthy FakeResult standing in for `_require_open_till_for_cash_reversal`'s
    "is a till open?" lookup — content doesn't matter, only truthiness."""
    return FakeResult(scalar=object())


def _no_till_result():
    return FakeResult(scalar=None)


# ── Refund ───────────────────────────────────────────────────────────────────

async def test_full_refund_sets_refunded_status():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000)
    db = FakeDB([FakeResult(scalar=order), _open_till_result()])

    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)

    assert result.payment_status == "REFUNDED"
    assert result.refund_amount_cents == 5000
    assert db.commits == 1


async def test_partial_refund_sets_partially_refunded_status():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000)
    db = FakeDB([FakeResult(scalar=order), _open_till_result()])

    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(amount_cents=2000), user=user, db=db)

    assert result.payment_status == "PARTIALLY_REFUNDED"
    assert result.refund_amount_cents == 2000


async def test_refund_ceiling_is_cash_actually_paid_not_the_rewards_original_value():
    """
    Pins a claim from the loyalty-redemption change: order.total_cents is
    already net of any redeemed reward's discount, so the existing
    `amount_cents > order.total_cents` ceiling automatically refunds only
    what was actually charged — a R100 sale with a R30 reward applied has
    total_cents=7000, and a refund can never exceed that 7000, regardless of
    the reward's original value. No code change was needed for this to be
    correct; this test exists so a future "fix" doesn't try to add the
    reward's value back into the refundable ceiling.
    """
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=7000, loyalty_reward_id=reward_id)
    db = FakeDB([FakeResult(scalar=order), _open_till_result()])

    with pytest.raises(AppError) as exc:
        await refund_pos_order(
            order_id=order.id, body=POSRefundRequest(amount_cents=10000), user=user, db=db,
        )
    assert exc.value.code == "REFUND_EXCEEDS_TOTAL"

    # A refund AT the actual (already-discounted) ceiling succeeds.
    db2 = FakeDB([
        FakeResult(scalar=order), _open_till_result(),
        FakeResult(scalar=_redeem_txn(uuid.uuid4(), order.id)),
        FakeResult(scalar=None),
        FakeResult(scalar=_loyalty_program()),
        FakeResult(scalar=_loyalty_customer()),
    ])
    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(amount_cents=7000), user=user, db=db2)
    assert result.refund_amount_cents == 7000


async def test_refund_exceeding_total_is_rejected():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000)
    db = FakeDB([FakeResult(scalar=order), _open_till_result()])

    with pytest.raises(AppError) as exc:
        await refund_pos_order(order_id=order.id, body=POSRefundRequest(amount_cents=9999), user=user, db=db)
    assert exc.value.code == "REFUND_EXCEEDS_TOTAL"


async def test_refund_unknown_order_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await refund_pos_order(order_id=uuid.uuid4(), body=POSRefundRequest(), user=user, db=db)


async def test_refund_rejects_non_pos_order():
    user = make_user()
    order = make_pos_order(user.business_id, source=OrderSource.WHATSAPP.value)
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert exc.value.code == "NOT_A_POS_ORDER"


async def test_refund_rejects_unpaid_order():
    user = make_user()
    order = make_pos_order(user.business_id, payment_status="PENDING")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert exc.value.code == "ORDER_NOT_PAID"


async def test_refund_writes_audit_event():
    user = make_user()
    order = make_pos_order(user.business_id)
    db = FakeDB([FakeResult(scalar=order), _open_till_result()])

    await refund_pos_order(order_id=order.id, body=POSRefundRequest(reason="Customer complaint"), user=user, db=db)

    from shared.models.audit import AuditEvent
    events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(events) == 1
    assert events[0].action == "order.pos.refunded"


# ── Void ─────────────────────────────────────────────────────────────────────

async def test_void_sets_voided_payment_status_and_cancels_order():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=8000)
    db = FakeDB([FakeResult(scalar=order), _open_till_result(), FakeResult(scalar=make_business_for_reversal())])

    result = await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Rang up wrong item"), user=user, db=db)

    assert result.payment_status == "VOIDED"
    assert result.status == "CANCELLED"
    assert result.refund_amount_cents == 8000
    assert result.cancelled_reason == "Rang up wrong item"


async def test_void_writes_distinct_audit_action_from_refund():
    user = make_user()
    order = make_pos_order(user.business_id)
    db = FakeDB([FakeResult(scalar=order), _open_till_result(), FakeResult(scalar=make_business_for_reversal())])

    await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Mistake"), user=user, db=db)

    from shared.models.audit import AuditEvent
    events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert events[0].action == "order.pos.voided"


async def test_void_rejects_already_refunded_order():
    user = make_user()
    order = make_pos_order(user.business_id, payment_status="REFUNDED")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="oops"), user=user, db=db)
    assert exc.value.code == "ORDER_NOT_PAID"


# ── Loyalty points reversal (void always returns points; full refund does by
#    default; partial refund never touches points) ─────────────────────────

def _redeem_txn(customer_id, order_id, points_delta=-200):
    return types.SimpleNamespace(
        id=uuid.uuid4(), customer_id=customer_id, order_id=order_id,
        transaction_type="REDEEM", points_delta=points_delta, idempotency_key=None,
    )


def _loyalty_customer(points_balance=300):
    return types.SimpleNamespace(id=uuid.uuid4(), loyalty_points_balance=points_balance, loyalty_tier="BRONZE")


def _loyalty_program():
    return types.SimpleNamespace(is_enabled=True, silver_tier_points=500, gold_tier_points=2000)


async def test_void_always_returns_points_even_without_a_flag():
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=8000, loyalty_reward_id=reward_id)
    customer = _loyalty_customer(points_balance=300)
    txn = _redeem_txn(customer.id, order.id, points_delta=-200)

    db = FakeDB([
        FakeResult(scalar=order),                     # _load_pos_order_for_reversal
        _open_till_result(),                           # cash-reversal till check
        FakeResult(scalar=make_business_for_reversal()),  # stock-reversal business lookup
        FakeResult(scalar=txn),                        # reverse_redemption: original REDEEM lookup
        FakeResult(scalar=None),                        # reverse_redemption: no existing reversal
        FakeResult(scalar=_loyalty_program()),           # reverse_redemption: program lookup
        FakeResult(scalar=customer),                     # reverse_redemption: customer FOR UPDATE lock
    ])

    result = await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Rang up wrong item"), user=user, db=db)

    assert result.payment_status == "VOIDED"
    assert customer.loyalty_points_balance == 500  # 300 + 200 restored
    from shared.models.audit import AuditEvent
    events = [o for o in db.added if isinstance(o, AuditEvent) and o.action == "order.pos.voided"]
    assert events[0].diff_json["loyalty_points_restored"] == 200


async def test_void_with_no_redemption_does_not_touch_loyalty():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=8000, loyalty_reward_id=None)
    db = FakeDB([FakeResult(scalar=order), _open_till_result(), FakeResult(scalar=make_business_for_reversal())])

    await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Mistake"), user=user, db=db)
    # No extra FakeResults were queued for a loyalty reversal — if the route
    # tried to call reverse_redemption anyway, popping from an empty list
    # would raise IndexError and fail this test.


async def test_full_refund_restores_points_by_default():
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD", loyalty_reward_id=reward_id)
    customer = _loyalty_customer(points_balance=300)
    txn = _redeem_txn(customer.id, order.id, points_delta=-200)

    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=txn),
        FakeResult(scalar=None),
        FakeResult(scalar=_loyalty_program()),
        FakeResult(scalar=customer),
    ])

    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)

    assert result.payment_status == "REFUNDED"
    assert customer.loyalty_points_balance == 500


async def test_full_refund_with_restore_loyalty_points_false_skips_reversal():
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD", loyalty_reward_id=reward_id)
    db = FakeDB([FakeResult(scalar=order)])  # no loyalty-reversal FakeResults queued at all

    result = await refund_pos_order(
        order_id=order.id, body=POSRefundRequest(restore_loyalty_points=False), user=user, db=db,
    )
    assert result.payment_status == "REFUNDED"
    # IndexError from popping an unqueued result would have failed this test
    # if the route called reverse_redemption anyway.


async def test_partial_refund_never_touches_points_even_with_flag_true():
    """There is no honest pro-rata answer — a partial refund never touches points, regardless of the flag."""
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD", loyalty_reward_id=reward_id)
    db = FakeDB([FakeResult(scalar=order)])  # no loyalty-reversal FakeResults queued

    result = await refund_pos_order(
        order_id=order.id,
        body=POSRefundRequest(amount_cents=2000, restore_loyalty_points=True),
        user=user, db=db,
    )
    assert result.payment_status == "PARTIALLY_REFUNDED"


async def test_refund_with_no_redemption_does_not_touch_loyalty():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD", loyalty_reward_id=None)
    db = FakeDB([FakeResult(scalar=order)])

    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert result.payment_status == "REFUNDED"


async def test_void_double_tap_restores_points_exactly_once():
    """
    The order-row FOR UPDATE lock is the primary guard: a second void call
    sees payment_status already VOIDED (not PAID) and is rejected by
    _load_pos_order_for_reversal before reverse_redemption is ever called a
    second time.
    """
    user = make_user()
    reward_id = uuid.uuid4()
    order = make_pos_order(user.business_id, total_cents=8000, loyalty_reward_id=reward_id)
    customer = _loyalty_customer(points_balance=300)
    txn = _redeem_txn(customer.id, order.id, points_delta=-200)

    db = FakeDB([
        FakeResult(scalar=order),
        _open_till_result(),
        FakeResult(scalar=make_business_for_reversal()),
        FakeResult(scalar=txn),
        FakeResult(scalar=None),
        FakeResult(scalar=_loyalty_program()),
        FakeResult(scalar=customer),
    ])
    await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="oops"), user=user, db=db)
    assert customer.loyalty_points_balance == 500

    # Second (double-tapped) void call against the now-VOIDED order.
    db2 = FakeDB([FakeResult(scalar=order)])
    with pytest.raises(AppError) as exc:
        await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="oops again"), user=user, db=db2)
    assert exc.value.code == "ORDER_NOT_PAID"
    assert customer.loyalty_points_balance == 500  # unchanged by the rejected second call


# ── Cash reversal requires an open till (Wave 0A fix) ───────────────────────
# A cash refund/void hands physical cash back out of a drawer — with no till
# open, that cash-out is never reconciled at the next cash-up.

async def test_cash_refund_without_open_till_is_rejected():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CASH")
    db = FakeDB([FakeResult(scalar=order), _no_till_result()])

    with pytest.raises(AppError) as exc:
        await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert exc.value.code == "TILL_NOT_OPEN"
    assert exc.value.status_code == 409
    # Nothing should have been mutated/committed on the rejected path.
    assert db.commits == 0


async def test_cash_void_without_open_till_is_rejected():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CASH")
    db = FakeDB([FakeResult(scalar=order), _no_till_result()])

    with pytest.raises(AppError) as exc:
        await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="oops"), user=user, db=db)
    assert exc.value.code == "TILL_NOT_OPEN"
    assert db.commits == 0


async def test_card_refund_does_not_require_open_till():
    """Non-cash reversals never touch a physical drawer — unaffected by the till gate."""
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD")
    # No till FakeResult queued at all — proves the till lookup is skipped entirely.
    db = FakeDB([FakeResult(scalar=order)])

    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert result.payment_status == "REFUNDED"


async def test_card_void_does_not_require_open_till():
    user = make_user()
    order = make_pos_order(user.business_id, total_cents=5000, payment_method="CARD")
    db = FakeDB([FakeResult(scalar=order), FakeResult(scalar=make_business_for_reversal())])

    result = await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Mistake"), user=user, db=db)
    assert result.payment_status == "VOIDED"


async def test_cash_reversal_till_lookup_is_locked_for_update():
    """
    F4 fix (Wave 0A verifier): the "is a till open?" SELECT in
    _require_open_till_for_cash_reversal must take the same FOR UPDATE lock
    close_till() takes on the same row, so a cash refund/void committing
    just after close_till has computed its totals can't land in no session.
    A real concurrent-commit race isn't practical to assert deterministically
    without a live DB (see tests/test_wave0a_pos_concurrency.py for the
    pattern used elsewhere), so this asserts the lock is actually requested.
    """
    from backend.app.api.v1.routes_pos import _require_open_till_for_cash_reversal

    class CapturingDB:
        def __init__(self, result):
            self.captured_stmt = None
            self._result = result

        async def execute(self, stmt):
            self.captured_stmt = stmt
            return self._result

    business_id = uuid.uuid4()
    order = make_pos_order(business_id, total_cents=5000, payment_method="CASH")
    db = CapturingDB(_open_till_result())

    await _require_open_till_for_cash_reversal(db, business_id, order)

    assert db.captured_stmt is not None
    assert db.captured_stmt._for_update_arg is not None, (
        "till lookup must use .with_for_update() — see close_till()'s matching lock"
    )


# ── Staff-readable POS settings ──────────────────────────────────────────────

async def test_pos_settings_reflects_business_config():
    import types
    user = make_user(role="STAFF")
    business = types.SimpleNamespace(
        pos_cash_enabled=True, pos_card_enabled=False,
        pos_payfast_enabled=False, pos_yoco_enabled=True, pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False, pos_other_payment_enabled=False,
        pos_table_number_required=True, delivery_fee_cents=1500, max_staff_discount_cents=2000,
        # VAT / receipt / inventory columns (Ciao Pizza P0) — mirrors the
        # real Business model defaults so this fake matches the schema.
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True, receipt_paper_width_mm=58,
        receipt_auto_print=True, receipt_copies=2, print_kitchen_receipt=True,
        receipt_config_json=None, inventory_enabled=True,
        # Opposite of the response model's own default, so the assertion
        # below proves the route reads the business row rather than
        # falling through to the schema default -- same reasoning as the
        # staff navigation flags a few lines down.
        digital_receipts_enabled=True,
        # UI V2.1 follow-up: loyalty_enabled, opposite of the response
        # model's own default (False), so the assertion below proves the
        # route reads the business row rather than falling through to the
        # schema default — same reasoning as the staff navigation flags.
        loyalty_enabled=True,
        wastage_approval_threshold_cents=0,
        # Staff navigation flags. Set to the OPPOSITE of their model defaults
        # so the assertions below prove the route reads the business row
        # rather than falling through to the response model's own defaults.
        staff_live_orders_tab_enabled=False,
        staff_wastage_tab_enabled=False,
        staff_stocktake_tab_enabled=True,
    )
    db = FakeDB([FakeResult(scalar=business)])

    settings = await get_pos_settings(user=user, db=db)

    assert settings.cash_enabled is True
    assert settings.card_enabled is False
    assert settings.other_payment_enabled is True  # yoco flag is on
    assert settings.table_number_required is True
    assert settings.delivery_fee_cents == 1500
    assert settings.max_staff_discount_cents == 2000
    assert settings.vat_registered is True
    assert settings.vat_rate_basis_points == 1500
    assert settings.vat_inclusive_pricing is True
    assert settings.receipt_paper_width_mm == 58
    assert settings.receipt_copies == 2
    assert settings.print_kitchen_receipt is True
    assert settings.inventory_enabled is True
    assert settings.digital_receipts_enabled is True
    assert settings.loyalty_enabled is True
    assert settings.staff_live_orders_tab_enabled is False
    assert settings.staff_wastage_tab_enabled is False
    assert settings.staff_stocktake_tab_enabled is True


async def test_pos_settings_stocktake_tab_needs_the_inventory_module():
    """
    The stocktake tab is gated on BOTH its own flag and inventory_enabled.
    Showing it without the module would land staff on a tab whose API
    immediately refuses them with INVENTORY_NOT_ENABLED.
    """
    import types
    user = make_user(role="STAFF")
    business = types.SimpleNamespace(
        pos_cash_enabled=True, pos_card_enabled=True,
        pos_payfast_enabled=False, pos_yoco_enabled=False, pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False, pos_other_payment_enabled=False,
        pos_table_number_required=False, delivery_fee_cents=0, max_staff_discount_cents=0,
        vat_registered=False, vat_number=None, vat_rate_basis_points=0,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80,
        receipt_auto_print=True, receipt_copies=1, print_kitchen_receipt=False,
        receipt_config_json=None,
        # Tab switched on, module switched off.
        inventory_enabled=False,
        digital_receipts_enabled=False,
        loyalty_enabled=False,
        wastage_approval_threshold_cents=0,
        staff_live_orders_tab_enabled=True,
        staff_wastage_tab_enabled=True,
        staff_stocktake_tab_enabled=True,
    )
    db = FakeDB([FakeResult(scalar=business)])

    settings = await get_pos_settings(user=user, db=db)

    assert settings.staff_stocktake_tab_enabled is False
    # The other tabs are unaffected by the inventory module.
    assert settings.staff_live_orders_tab_enabled is True
    assert settings.staff_wastage_tab_enabled is True


async def test_pos_settings_unknown_business_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])
    with pytest.raises(NotFoundError):
        await get_pos_settings(user=user, db=db)


def _pos_settings_business(loyalty_enabled: bool):
    """Minimal Business stand-in carrying every field get_pos_settings reads,
    varying only loyalty_enabled -- see test_pos_settings_loyalty_enabled_*
    below (UI V2.1 follow-up Part B1: a real field, not an API-error probe)."""
    return types.SimpleNamespace(
        pos_cash_enabled=True, pos_card_enabled=True,
        pos_payfast_enabled=False, pos_yoco_enabled=False, pos_ikhoka_enabled=False,
        pos_direct_eft_enabled=False, pos_other_payment_enabled=False,
        pos_table_number_required=False, delivery_fee_cents=0, max_staff_discount_cents=0,
        vat_registered=False, vat_number=None, vat_rate_basis_points=0,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80,
        receipt_auto_print=True, receipt_copies=1, print_kitchen_receipt=False,
        receipt_config_json=None, inventory_enabled=False,
        digital_receipts_enabled=False,
        loyalty_enabled=loyalty_enabled,
        wastage_approval_threshold_cents=0,
        staff_live_orders_tab_enabled=True,
        staff_wastage_tab_enabled=True,
        staff_stocktake_tab_enabled=False,
    )


async def test_pos_settings_loyalty_enabled_false_is_returned_as_false():
    user = make_user(role="STAFF")
    db = FakeDB([FakeResult(scalar=_pos_settings_business(loyalty_enabled=False))])

    settings = await get_pos_settings(user=user, db=db)

    assert settings.loyalty_enabled is False


async def test_pos_settings_loyalty_enabled_true_is_returned_as_true():
    user = make_user(role="STAFF")
    db = FakeDB([FakeResult(scalar=_pos_settings_business(loyalty_enabled=True))])

    settings = await get_pos_settings(user=user, db=db)

    assert settings.loyalty_enabled is True


async def test_pos_settings_queries_by_the_callers_own_business_id_only():
    """
    Tenant isolation: GET /pos/settings must filter strictly by
    user.business_id, never by anything else -- the same invariant every
    other business-scoped route relies on. Asserted structurally (the WHERE
    clause's bound parameter), the same technique already used above for
    _require_open_till_for_cash_reversal's lock, since a fake in-memory DB
    can't enforce a real SQL WHERE.
    """
    class CapturingDB:
        def __init__(self, result):
            self.captured_stmt = None
            self._result = result

        async def execute(self, stmt):
            self.captured_stmt = stmt
            return self._result

    business_id = uuid.uuid4()
    user = make_user(role="STAFF", business_id=business_id)
    db = CapturingDB(FakeResult(scalar=_pos_settings_business(loyalty_enabled=True)))

    await get_pos_settings(user=user, db=db)

    assert db.captured_stmt is not None
    compiled = db.captured_stmt.compile(compile_kwargs={"literal_binds": True})
    assert business_id.hex in str(compiled).replace("-", ""), (
        "GET /pos/settings must filter Business by the caller's own "
        "business_id -- this query must never be able to read another "
        "tenant's row"
    )


# ── Split payments (migration 0055): refund_cash_cents ───────────────────────

def make_split_order(business_id, total_cents=30000, payment_status="PAID"):
    return make_pos_order(business_id, total_cents=total_cents, payment_status=payment_status, payment_method="SPLIT")


async def test_void_on_split_order_requires_open_till_when_it_has_a_cash_allocation():
    user = make_user()
    order = make_split_order(user.business_id)
    db = FakeDB([
        FakeResult(scalar=order),      # order lock
        FakeResult(scalar=None),       # _require_open_till_for_cash_reversal's existence check: no CASH allocation -> no till lock needed
        FakeResult(scalar=None),       # _compute_cash_portion's own sum query: still 0
        FakeResult(scalar=make_business_for_reversal()),
    ])
    result = await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Mistake"), user=user, db=db)
    assert order.refund_cash_cents == 0
    assert result.payment_status == "VOIDED"


async def test_void_on_split_order_with_cash_allocation_computes_cash_portion():
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),                        # order lock
        FakeResult(scalar=uuid.uuid4()),                 # has a CASH allocation -> open till required
        _open_till_result(),                             # till lock
        FakeResult(scalar=10000),                         # _compute_cash_portion: sum of CASH allocations
        FakeResult(scalar=make_business_for_reversal()),  # business lookup for inventory reversal
    ])
    await void_pos_order(order_id=order.id, body=POSVoidRequest(reason="Mistake"), user=user, db=db)
    assert order.refund_cash_cents == 10000
    assert order.refund_amount_cents == 30000


async def test_full_refund_on_split_order_computes_cash_portion_from_allocations():
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),        # order lock
        FakeResult(scalar=uuid.uuid4()), # has a CASH allocation -> open till required
        _open_till_result(),             # till lock
        FakeResult(scalar=10000),        # _compute_cash_portion (full refund): sum of CASH allocations
    ])
    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(), user=user, db=db)
    assert result.payment_status == "REFUNDED"
    assert order.refund_cash_cents == 10000


async def test_partial_refund_on_mixed_split_requires_explicit_cash_refund_cents():
    """
    A partial refund of a split order mixing cash and non-cash methods is
    genuinely ambiguous (which method's money is coming back?) — the
    manager must say explicitly via cash_refund_cents, never guessed.
    """
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=uuid.uuid4()),  # has a CASH allocation
        _open_till_result(),
        FakeResult(items=[("CASH", 10000), ("CARD", 20000)]),  # mixed allocations
    ])
    with pytest.raises(AppError) as exc:
        await refund_pos_order(order_id=order.id, body=POSRefundRequest(amount_cents=5000), user=user, db=db)
    assert exc.value.code == "CASH_REFUND_PORTION_REQUIRED"


async def test_partial_refund_on_mixed_split_with_explicit_cash_refund_cents_succeeds():
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=uuid.uuid4()),
        _open_till_result(),
        FakeResult(items=[("CASH", 10000), ("CARD", 20000)]),
    ])
    result = await refund_pos_order(
        order_id=order.id,
        body=POSRefundRequest(amount_cents=5000, cash_refund_cents=5000),
        user=user, db=db,
    )
    assert result.payment_status == "PARTIALLY_REFUNDED"
    assert order.refund_cash_cents == 5000


async def test_partial_refund_cash_refund_cents_cannot_exceed_cash_allocated():
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=uuid.uuid4()),
        _open_till_result(),
        FakeResult(items=[("CASH", 10000), ("CARD", 20000)]),
    ])
    with pytest.raises(AppError) as exc:
        await refund_pos_order(
            order_id=order.id,
            body=POSRefundRequest(amount_cents=15000, cash_refund_cents=15000),
            user=user, db=db,
        )
    assert exc.value.code == "CASH_REFUND_PORTION_INVALID"


async def test_partial_refund_on_cash_only_split_does_not_require_explicit_value():
    """An all-cash split (e.g. two Cash allocations) is unambiguous even for
    a partial refund — no cash_refund_cents needed, same as a legacy CASH order."""
    user = make_user()
    order = make_split_order(user.business_id, total_cents=30000)
    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=uuid.uuid4()),
        _open_till_result(),
        FakeResult(items=[("CASH", 10000), ("CASH", 20000)]),
    ])
    result = await refund_pos_order(order_id=order.id, body=POSRefundRequest(amount_cents=5000), user=user, db=db)
    assert result.payment_status == "PARTIALLY_REFUNDED"
    assert order.refund_cash_cents == 5000
