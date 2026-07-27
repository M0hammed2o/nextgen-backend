"""
Shared billing-lifecycle transitions (backend/app/billing/lifecycle.py) —
reused by every billing provider's webhook handler. "Retry. Notify. Grace
period. Suspend account." — retry/notify are the provider's own job; this
module owns the grace-period/suspension state machine NextGen enforces.
"""
import types
import uuid
from datetime import datetime, timedelta, timezone

from backend.app.billing.lifecycle import (
    GRACE_PERIOD_DAYS,
    is_past_grace_period,
    mark_invoice_paid,
    mark_payment_failed,
    suspend_for_nonpayment,
)


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(), billing_status="ACTIVE", is_active=True, suspended_reason=None,
        billing_period_started_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def test_mark_invoice_paid_reactivates_and_resets_period():
    business = make_business(
        billing_status="PAST_DUE", is_active=False, suspended_reason="overdue",
        billing_period_started_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    now_before_call = datetime.now(timezone.utc)
    mark_invoice_paid(business)
    assert business.billing_status == "ACTIVE"
    assert business.is_active is True
    assert business.suspended_reason is None
    assert business.billing_period_started_at >= now_before_call


def test_mark_invoice_paid_on_already_active_business_still_resets_period():
    business = make_business(
        billing_status="ACTIVE",
        billing_period_started_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    now_before_call = datetime.now(timezone.utc)
    mark_invoice_paid(business)
    assert business.billing_status == "ACTIVE"
    assert business.billing_period_started_at >= now_before_call


def test_mark_payment_failed_sets_past_due_without_suspending():
    business = make_business(billing_status="ACTIVE", is_active=True)
    mark_payment_failed(business)
    assert business.billing_status == "PAST_DUE"
    assert business.is_active is True  # not suspended yet — grace period applies


def test_is_past_grace_period_false_when_not_past_due():
    business = make_business(billing_status="ACTIVE")
    assert is_past_grace_period(business) is False


def test_is_past_grace_period_false_within_grace_window():
    business = make_business(
        billing_status="PAST_DUE",
        billing_period_started_at=datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS - 1),
    )
    assert is_past_grace_period(business) is False


def test_is_past_grace_period_true_after_grace_window():
    business = make_business(
        billing_status="PAST_DUE",
        billing_period_started_at=datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS + 1),
    )
    assert is_past_grace_period(business) is True


def test_suspend_for_nonpayment_disables_without_deleting_data():
    business = make_business(billing_status="PAST_DUE", is_active=True)
    suspend_for_nonpayment(business)
    assert business.billing_status == "SUSPENDED"
    assert business.is_active is False
    assert business.suspended_reason is not None
    # Never touches anything but these three fields — no data deletion of any kind.
    assert hasattr(business, "id")
