"""
Unit tests for backend/app/billing/account_status.py (the single derived
commercial-status function) and backend/app/billing/trials.py (trial start/
extend). Pure Python — no DB needed, since these are plain functions over
in-memory business/invoice objects (SimpleNamespace stand-ins, same style as
this module's own manual smoke-testing during development).
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.billing.account_status import compute_account_status
from backend.app.billing.trials import extend_trial, start_trial
from backend.app.core.errors import AppError

TZ = "Africa/Johannesburg"  # UTC+2, no DST — the platform default


def _biz(**overrides):
    defaults = dict(
        timezone=TZ, is_active=True, billing_status="TRIAL", trial_ends_at=None,
        trial_started_at=None, trial_extension_days_total=0, suspended_reason=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _inv(due_date):
    return SimpleNamespace(due_date=due_date)


# ── Precedence ────────────────────────────────────────────────────────────────

def test_suspended_takes_precedence_over_everything():
    biz = _biz(is_active=False, billing_status="TRIAL")
    acct = compute_account_status(biz, [], datetime.now(timezone.utc))
    assert acct.status == "SUSPENDED"
    assert acct.alerts == ["SUSPENDED"]
    assert acct.trial_days_remaining is None


def test_cancelled_status():
    biz = _biz(is_active=True, billing_status="CANCELLED")
    acct = compute_account_status(biz, [], datetime.now(timezone.utc))
    assert acct.status == "CANCELLED"
    assert acct.alerts == []


def test_past_due_billing_status_alone_is_overdue():
    biz = _biz(billing_status="PAST_DUE")
    acct = compute_account_status(biz, [], datetime.now(timezone.utc))
    assert acct.status == "OVERDUE"
    assert "INVOICE_OVERDUE" in acct.alerts


def test_active_with_no_open_invoices():
    biz = _biz(billing_status="ACTIVE")
    acct = compute_account_status(biz, [], datetime.now(timezone.utc))
    assert acct.status == "ACTIVE"


def test_payment_due_invoice_not_yet_due():
    biz = _biz(billing_status="ACTIVE")
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    acct = compute_account_status(biz, [_inv(date(2026, 9, 25))], now)
    assert acct.status == "PAYMENT_DUE"
    assert acct.alerts == ["PAYMENT_AWAITING"]


def test_overdue_invoice_past_due_date():
    biz = _biz(billing_status="ACTIVE")
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    acct = compute_account_status(biz, [_inv(date(2026, 9, 10))], now)
    assert acct.status == "OVERDUE"
    assert acct.alerts == ["INVOICE_OVERDUE"]


def test_overdue_wins_over_payment_due_when_both_present():
    biz = _biz(billing_status="ACTIVE")
    now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    acct = compute_account_status(biz, [_inv(date(2026, 9, 25)), _inv(date(2026, 9, 10))], now)
    assert acct.status == "OVERDUE"


# ── Trial day-threshold alerts: 8/7/3/1/0 days remaining ───────────────────────

@pytest.mark.parametrize("offset_days,expected_remaining,expected_alert", [
    (0, 14, None),
    (6, 8, None),           # 8 days left -> no alert (8 > 7)
    (7, 7, "TRIAL_ENDS_7D"),
    (11, 3, "TRIAL_ENDS_3D"),
    (13, 1, "TRIAL_ENDS_1D"),
    (14, 0, "TRIAL_ENDS_1D"),  # last day, 0 remaining, still the most urgent alert
])
def test_trial_alert_thresholds(offset_days, expected_remaining, expected_alert):
    biz = _biz()
    start = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    start_trial(biz, now=start)
    probe = start + timedelta(days=offset_days)
    acct = compute_account_status(biz, [], probe)
    assert acct.status == "TRIAL"
    assert acct.trial_days_remaining == expected_remaining
    if expected_alert:
        assert acct.alerts == [expected_alert]
    else:
        assert acct.alerts == []


def test_trial_expires_the_instant_it_passes():
    biz = _biz()
    start = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    start_trial(biz, now=start)
    probe = biz.trial_ends_at + timedelta(microseconds=1)
    acct = compute_account_status(biz, [], probe)
    assert acct.status == "EXPIRED"
    assert acct.trial_days_remaining == 0
    assert acct.alerts == ["TRIAL_EXPIRED"]


def test_trial_utc_plus_2_near_midnight_does_not_undercount():
    """A business at 23:50 local, exactly 7 local calendar days before
    expiry, must read as 7 days remaining — not shifted by the UTC+2 offset
    (this is the exact class of bug BUG-0032 was, in a different function)."""
    biz = _biz()
    start = datetime(2026, 9, 14, 21, 50, tzinfo=timezone.utc)  # 23:50 local
    start_trial(biz, now=start)
    probe = datetime(2026, 9, 21, 21, 50, tzinfo=timezone.utc)  # 23:50 local, 7 days later
    acct = compute_account_status(biz, [], probe)
    assert acct.trial_days_remaining == 7
    assert acct.alerts == ["TRIAL_ENDS_7D"]


# ── Trial start/extend ──────────────────────────────────────────────────────

def test_start_trial_sets_fields_and_resets_extension_counter():
    biz = _biz(trial_extension_days_total=5)
    now = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    start_trial(biz, now=now)
    assert biz.billing_status == "TRIAL"
    assert biz.trial_started_at == now
    assert biz.trial_extension_days_total == 0
    assert biz.trial_ends_at is not None


def test_onboarder_extension_cap_enforced():
    biz = _biz()
    start_trial(biz, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    extend_trial(biz, 10, is_onboarder=True)
    assert biz.trial_extension_days_total == 10
    with pytest.raises(AppError) as exc:
        extend_trial(biz, 10, is_onboarder=True)  # 10+10=20 > cap of 14
    assert exc.value.code == "EXTENSION_CAP_EXCEEDED"
    assert exc.value.status_code == 403
    # exactly at the cap succeeds
    extend_trial(biz, 4, is_onboarder=True)
    assert biz.trial_extension_days_total == 14


def test_super_admin_extension_is_unlimited():
    biz = _biz()
    start_trial(biz, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    extend_trial(biz, 1000, is_onboarder=False)
    assert biz.trial_extension_days_total == 1000


def test_extend_reactivates_and_unsuspends_an_expired_trial():
    biz = _biz()
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    start_trial(biz, now=now)
    expired_now = now + timedelta(days=20)
    assert compute_account_status(biz, [], expired_now).status == "EXPIRED"

    # simulate the cron having suspended it for TRIAL_EXPIRED
    biz.is_active = False
    biz.suspended_reason = "TRIAL_EXPIRED"

    extend_trial(biz, 30, is_onboarder=False, now=expired_now)
    assert biz.is_active is True
    assert biz.suspended_reason is None
    assert compute_account_status(biz, [], expired_now).status == "TRIAL"


def test_extend_never_unsuspends_a_non_trial_suspension():
    biz = _biz()
    start_trial(biz, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    biz.is_active = False
    biz.suspended_reason = "Manually suspended for abuse"
    extend_trial(biz, 5, is_onboarder=False)
    assert biz.is_active is False
    assert biz.suspended_reason == "Manually suspended for abuse"


def test_extend_requires_positive_days():
    biz = _biz()
    start_trial(biz, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
    with pytest.raises(AppError):
        extend_trial(biz, 0, is_onboarder=False)
    with pytest.raises(AppError):
        extend_trial(biz, -1, is_onboarder=False)
