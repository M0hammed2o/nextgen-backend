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
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


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
                    payment_method="CASH"):
    now = datetime.now(timezone.utc)
    return Order(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status="ACCEPTED", payment_status=payment_status, payment_method=payment_method,
        source=source, total_cents=total_cents, subtotal_cents=total_cents,
        currency="ZAR",
        payment_required=False, order_mode="PICKUP", delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, created_at=now, updated_at=now,
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
