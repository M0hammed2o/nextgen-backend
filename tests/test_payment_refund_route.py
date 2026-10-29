"""
POST /business/orders/{id}/refund and GET /business/payments/health —
the routes that expose the Payment Provider Framework's refund_payment()/
check_health() to the manager dashboard. Uses the MOCK provider (real,
deterministic, no network) rather than mocking httpx again — the per-provider
HTTP-call correctness is already covered in test_payment_provider_framework.py.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_business import get_payment_provider_health
from backend.app.api.v1.routes_orders import RefundRequest, refund_order_payment
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent


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


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_order(business_id, **overrides):
    o = types.SimpleNamespace(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        payment_status="PAID", total_cents=9000, payment_reference=None,
        refund_reference=None, refund_amount_cents=None, refunded_at=None,
        status="NEW", payment_required=False, payment_method="CARD",
        payment_link_url=None, paid_at=None, order_mode="PICKUP", source="POS",
        subtotal_cents=9000, delivery_fee_cents=0, discount_cents=0, tax_cents=0,
        discount_reason=None, currency="ZAR", customer_name=None, phone_number=None,
        delivery_address=None, estimated_ready_at=None, confirmed_at=None,
        accepted_at=None, ready_at=None, completed_at=None, special_instructions=None,
        cancelled_reason=None, items=[], created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


def make_business(business_id, **overrides):
    b = types.SimpleNamespace(id=business_id, payment_provider="MOCK")
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ── Refund route ─────────────────────────────────────────────────────────────

async def test_refund_success_with_mock_provider_marks_order_refunded():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID")
    business = make_business(user.business_id, payment_provider="MOCK")
    db = FakeDB([FakeResult(scalar=order), FakeResult(scalar=business)])

    result = await refund_order_payment(order_id=order.id, body=RefundRequest(), user=user, db=db)

    assert result.success is True
    assert result.status == "COMPLETED"
    assert order.payment_status == "REFUNDED"
    assert order.refund_amount_cents == 9000
    assert db.commits == 1
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].action == "order.payment.refund_attempted"


async def test_refund_manual_required_does_not_change_payment_status():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID")
    business = make_business(user.business_id, payment_provider="DIRECT_EFT")
    db = FakeDB([FakeResult(scalar=order), FakeResult(scalar=business)])

    result = await refund_order_payment(order_id=order.id, body=RefundRequest(), user=user, db=db)

    assert result.success is False
    assert result.status == "MANUAL_REQUIRED"
    assert order.payment_status == "PAID"  # unchanged — money hasn't actually moved


async def test_refund_rejects_order_that_is_not_paid():
    user = make_user()
    order = make_order(user.business_id, payment_status="PENDING")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(order_id=order.id, body=RefundRequest(), user=user, db=db)
    assert exc.value.code == "ORDER_NOT_PAID"


# ── Rejected: a marketplace order's payment was collected externally ───────
# Mirrors the same guard added to PATCH .../payment — see
# test_payment_status_transitions.py and KNOWN_BUGS.md, found during the
# delivery-integration Phase 1 audit, 2026-10-26. Only one FakeResult is
# queued because the guard must raise before the route's second query
# (fetching the business's payment provider) ever runs.

async def test_refund_rejects_a_uber_eats_order():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", source="UBER_EATS")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(order_id=order.id, body=RefundRequest(), user=user, db=db)
    assert exc.value.code == "EXTERNAL_ORDER_PAYMENT_LOCKED"
    assert db.commits == 0


async def test_refund_rejects_a_mr_d_order_even_with_confirm_manual():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", source="MR_D")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id,
            body=RefundRequest(confirm_manual=True, reference="test"),
            user=user, db=db,
        )
    assert exc.value.code == "EXTERNAL_ORDER_PAYMENT_LOCKED"


async def test_refund_rejects_amount_exceeding_total():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", total_cents=5000)
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id, body=RefundRequest(amount_cents=9999), user=user, db=db,
        )
    assert exc.value.code == "REFUND_EXCEEDS_TOTAL"


async def test_refund_rejects_business_with_no_provider_configured():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID")
    business = make_business(user.business_id, payment_provider=None)
    db = FakeDB([FakeResult(scalar=order), FakeResult(scalar=business)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(order_id=order.id, body=RefundRequest(), user=user, db=db)
    assert exc.value.code == "NO_PAYMENT_PROVIDER"


async def test_refund_unknown_order_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await refund_order_payment(order_id=uuid.uuid4(), body=RefundRequest(), user=user, db=db)


# ── Manual refund confirmation (F3 fix, Wave 0A verifier) ──────────────────
# For MANUAL_REQUIRED providers (Direct EFT, Stitch), refund_payment() never
# sets REFUNDED and PATCH .../payment intentionally rejects it — this is the
# ONLY way staff can record that a manual refund actually happened.

async def test_manual_refund_confirm_records_full_refund():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", total_cents=9000)
    db = FakeDB([FakeResult(scalar=order)])  # no provider/business lookup at all

    result = await refund_order_payment(
        order_id=order.id,
        body=RefundRequest(confirm_manual=True, reference="EFT-POP-123", note="Confirmed by owner"),
        user=user, db=db,
    )

    assert result.success is True
    assert result.status == "REFUNDED"
    assert order.payment_status == "REFUNDED"
    assert order.refund_amount_cents == 9000
    assert order.refund_reference == "EFT-POP-123"
    assert order.refunded_at is not None
    assert db.commits == 1
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].action == "order.payment.manual_refund_confirmed"
    assert audit_events[0].diff_json["reference"] == "EFT-POP-123"


async def test_manual_refund_confirm_partial_amount_sets_partially_refunded():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", total_cents=9000)
    db = FakeDB([FakeResult(scalar=order)])

    result = await refund_order_payment(
        order_id=order.id,
        body=RefundRequest(confirm_manual=True, amount_cents=3000, reference="EFT-POP-456"),
        user=user, db=db,
    )

    assert result.status == "PARTIALLY_REFUNDED"
    assert order.payment_status == "PARTIALLY_REFUNDED"
    assert order.refund_amount_cents == 3000


async def test_manual_refund_confirm_rejects_amount_exceeding_total():
    user = make_user()
    order = make_order(user.business_id, payment_status="PAID", total_cents=5000)
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id,
            body=RefundRequest(confirm_manual=True, amount_cents=9999, reference="ref"),
            user=user, db=db,
        )
    assert exc.value.code == "REFUND_EXCEEDS_TOTAL"


async def test_manual_refund_confirm_rejects_unpaid_order():
    user = make_user()
    order = make_order(user.business_id, payment_status="PENDING")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id,
            body=RefundRequest(confirm_manual=True, reference="ref"),
            user=user, db=db,
        )
    assert exc.value.code == "ORDER_NOT_PAID"


async def test_manual_refund_confirm_is_idempotent_second_call_is_409():
    user = make_user()
    order = make_order(user.business_id, payment_status="REFUNDED")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id,
            body=RefundRequest(confirm_manual=True, reference="second-attempt"),
            user=user, db=db,
        )
    assert exc.value.code == "REFUND_ALREADY_RECORDED"
    assert exc.value.status_code == 409
    assert db.commits == 0


async def test_manual_refund_confirm_partially_refunded_also_rejects_second_call():
    user = make_user()
    order = make_order(user.business_id, payment_status="PARTIALLY_REFUNDED")
    db = FakeDB([FakeResult(scalar=order)])

    with pytest.raises(AppError) as exc:
        await refund_order_payment(
            order_id=order.id,
            body=RefundRequest(confirm_manual=True, reference="second-attempt"),
            user=user, db=db,
        )
    assert exc.value.code == "REFUND_ALREADY_RECORDED"
    assert exc.value.status_code == 409


def test_refund_request_confirm_manual_requires_reference():
    with pytest.raises(Exception):
        RefundRequest(confirm_manual=True)


def test_refund_request_confirm_manual_with_reference_is_valid():
    body = RefundRequest(confirm_manual=True, reference="EFT-1")
    assert body.confirm_manual is True
    assert body.reference == "EFT-1"


# ── Payment provider health route ───────────────────────────────────────────

async def test_health_no_provider_configured():
    user = make_user()
    business = make_business(user.business_id, payment_provider=None)
    db = FakeDB([FakeResult(scalar=business)])

    result = await get_payment_provider_health(user=user, db=db)
    assert result.healthy is False
    assert result.provider == "NONE"


async def test_health_with_mock_provider_configured():
    user = make_user()
    business = make_business(user.business_id, payment_provider="MOCK")
    db = FakeDB([FakeResult(scalar=business)])

    result = await get_payment_provider_health(user=user, db=db)
    assert result.healthy is True
    assert result.provider == "MOCK"


async def test_health_unknown_business_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await get_payment_provider_health(user=user, db=db)
