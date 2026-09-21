"""
PATCH /business/orders/{id}/payment accepting payment_method="MANUAL"
(Wave 0A P1 fix).

The staff app's mark-paid button, shown on an order awaiting ONLINE
payment (payment_required=True), sends payment_method="MANUAL" — see
nextgen-staff-v3/.../StatusActions.tsx's awaitingOnlinePayment branch. The
backend's PaymentUpdateRequest.payment_method pattern didn't include
"MANUAL", so every tap of that button 422'd. Fixed by accepting MANUAL as
an explicit payment method meaning "confirmed manually by staff", recorded
as an OrderEvent — without loosening the payment-status transition rules
from commit 48d2ffe (staff may only move to PAID; REFUNDED/VOIDED still
rejected here).
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_orders import PaymentUpdateRequest, update_payment_status
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.order import Order, OrderEvent


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


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_order(business_id, payment_status="UNPAID", **overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status="NEW", payment_status=payment_status, payment_method=None,
        payment_reference=None, payment_link_url=None, payment_required=True,
        paid_at=None, order_mode="PICKUP", table_number=None, source="WHATSAPP",
        cash_tendered_cents=None, change_due_cents=None, till_session_id=None,
        created_by_user_id=None, subtotal_cents=5000, delivery_fee_cents=0,
        discount_cents=0, tax_cents=0, discount_reason=None, total_cents=5000,
        currency="ZAR", refund_reference=None, refund_amount_cents=None, refunded_at=None,
        customer_name=None, phone_number=None, delivery_address=None,
        estimated_ready_at=None, confirmed_at=None, accepted_at=None, ready_at=None,
        completed_at=None, special_instructions=None, cancelled_reason=None,
        customer_id=None, created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    order = Order()
    for k, v in defaults.items():
        setattr(order, k, v)
    order.items = []
    return order


def _events(db: FakeDB):
    return [obj for obj in db.added if isinstance(obj, OrderEvent)]


# ── This is the exact request the staff app sends — must no longer 422 ─────

def test_manual_is_a_valid_payment_method_value():
    """Previously: ValidationError (422) before the route even ran."""
    req = PaymentUpdateRequest(payment_status="PAID", payment_method="MANUAL")
    assert req.payment_method == "MANUAL"


async def test_staff_can_mark_paid_manually_on_awaiting_online_payment_order():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PENDING", payment_required=True)
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="MANUAL"),
        user=user, db=db,
    )

    assert result.payment_status == "PAID"
    assert result.payment_method == "MANUAL"
    assert db.commits == 1


async def test_manual_mark_paid_writes_an_order_event():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="UNPAID", payment_required=True)
    db = FakeDB([FakeResult(scalar=order)])

    await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="MANUAL"),
        user=user, db=db,
    )

    events = _events(db)
    assert len(events) == 1
    assert events[0].order_id == order.id
    assert events[0].changed_by_user_id == user.user_id
    assert "manually" in events[0].reason.lower()


async def test_non_manual_payment_method_writes_no_order_event():
    """No behaviour change for the existing CASH/CARD/etc. path."""
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="UNPAID", payment_required=False)
    db = FakeDB([FakeResult(scalar=order)])

    await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"),
        user=user, db=db,
    )

    assert _events(db) == []


# ── Transition rules from commit 48d2ffe are untouched by this change ──────

async def test_manual_cannot_be_used_to_reach_refunded():
    """REFUNDED must still be rejected here, MANUAL or not."""
    user = make_user(role="OWNER")
    order = make_order(user.business_id, payment_status="PAID", payment_required=True)
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await update_payment_status(
            order_id=order.id,
            body=PaymentUpdateRequest(payment_status="REFUNDED", payment_method="MANUAL"),
            user=user, db=db,
        )
    assert exc.value.code == "USE_REFUND_ENDPOINT"


async def test_staff_still_cannot_revert_a_paid_order_even_with_manual():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PAID", payment_required=True)
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await update_payment_status(
            order_id=order.id,
            body=PaymentUpdateRequest(payment_status="FAILED", payment_method="MANUAL"),
            user=user, db=db,
        )
    assert exc.value.code == "OWNER_OR_MANAGER_REQUIRED"


def test_still_rejects_unknown_payment_method_values():
    with pytest.raises(Exception):
        PaymentUpdateRequest(payment_status="PAID", payment_method="BITCOIN")
