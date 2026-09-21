"""
PATCH /business/orders/{id}/payment — transition restrictions (Wave 0A).

Before this fix, `update_payment_status` accepted ANY payment_status a
staff member sent (including REFUNDED with no captured amount, and moving
a PAID order back to UNPAID with no audit trail or extra permission). This
proves the tightened contract:

  - UNPAID/PENDING/FAILED/CASH_ON_COLLECTION -> PAID: allowed for STAFF and
    above (the only transition the staff app actually performs — see
    StatusActions.tsx's two PATCH .../payment call sites, both
    payment_status="PAID").
  - REFUNDED/PARTIALLY_REFUNDED/VOIDED: rejected here entirely, with a
    clear message pointing at the refund/void endpoints.
  - PAID -> any non-PAID status: requires OWNER/MANAGER, and is audited.
  - PAID -> PAID (idempotent re-mark): allowed for STAFF, a no-op.
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_orders import PaymentUpdateRequest, update_payment_status
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
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


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_order(business_id, payment_status="UNPAID", **overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status="NEW", payment_status=payment_status, payment_method=None,
        payment_reference=None, payment_link_url=None, payment_required=False,
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


# ── Allowed: staff records payment at collection ────────────────────────────

async def test_staff_can_mark_unpaid_order_paid():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="UNPAID")
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"),
        user=user, db=db,
    )
    assert result.payment_status == "PAID"
    assert db.commits == 1


async def test_staff_can_mark_pending_order_paid():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PENDING")
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="CARD"),
        user=user, db=db,
    )
    assert result.payment_status == "PAID"


async def test_staff_re_marking_already_paid_order_paid_is_a_noop_allowed():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="PAID", payment_method="CASH"),
        user=user, db=db,
    )
    assert result.payment_status == "PAID"
    # No "reverted from paid" audit event for a PAID -> PAID no-op.
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert audit_events == []


# ── Rejected: refunds/voids must go through their dedicated endpoints ──────

@pytest.mark.parametrize("status", ["REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"])
async def test_refund_family_statuses_are_rejected(status):
    user = make_user(role="OWNER")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await update_payment_status(
            order_id=order.id,
            body=PaymentUpdateRequest(payment_status=status),
            user=user, db=db,
        )
    assert exc.value.code == "USE_REFUND_ENDPOINT"
    assert exc.value.status_code == 422
    assert db.commits == 0


# ── Restricted: reversing an already-PAID order needs owner/manager ────────

async def test_staff_cannot_move_paid_order_back_to_unpaid():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await update_payment_status(
            order_id=order.id,
            body=PaymentUpdateRequest(payment_status="UNPAID"),
            user=user, db=db,
        )
    assert exc.value.code == "OWNER_OR_MANAGER_REQUIRED"
    assert exc.value.status_code == 403
    assert db.commits == 0


async def test_staff_cannot_move_paid_order_to_failed():
    user = make_user(role="STAFF")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await update_payment_status(
            order_id=order.id,
            body=PaymentUpdateRequest(payment_status="FAILED"),
            user=user, db=db,
        )
    assert exc.value.code == "OWNER_OR_MANAGER_REQUIRED"


async def test_manager_can_move_paid_order_back_to_unpaid_and_it_is_audited():
    user = make_user(role="MANAGER")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="UNPAID"),
        user=user, db=db,
    )
    assert result.payment_status == "UNPAID"
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].action == "order.payment.reverted_from_paid"
    assert audit_events[0].diff_json["old_payment_status"] == "PAID"
    assert audit_events[0].diff_json["new_payment_status"] == "UNPAID"


async def test_owner_can_move_paid_order_back_to_unpaid():
    user = make_user(role="OWNER")
    order = make_order(user.business_id, payment_status="PAID")
    db = FakeDB([FakeResult(scalar=order)])

    result = await update_payment_status(
        order_id=order.id,
        body=PaymentUpdateRequest(payment_status="UNPAID"),
        user=user, db=db,
    )
    assert result.payment_status == "UNPAID"


async def test_unknown_order_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])
    with pytest.raises(NotFoundError):
        await update_payment_status(
            order_id=uuid.uuid4(),
            body=PaymentUpdateRequest(payment_status="PAID"),
            user=user, db=db,
        )
