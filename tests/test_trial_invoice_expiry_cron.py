"""
backend/app/worker.py::sweep_trial_and_invoice_expiry — the companion hourly
cron to suspend_past_due_businesses (see that function's own docstring NOTE
for why this is a separate function rather than inlined into it: reusing
the existing test file's single-shared-mock-result pattern across multiple
distinct db.execute() calls in one job body was verified to corrupt those
pre-existing tests). Same AsyncMock/session_factory pattern as
test_billing_suspension_cron.py and test_arq_worker.py.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from backend.app.core.config import get_settings
from backend.app.worker import WorkerSettings, sweep_trial_and_invoice_expiry

settings = get_settings()


def _make_ctx(business_result, invoice_rows):
    """Two distinct db.execute() calls happen in this job: (1) the TRIAL
    business query, (2) sweep_overdue_invoices' own invoice query. Use
    side_effect to give each call its own canned result, avoiding the exact
    cross-call-contamination pitfall documented in worker.py."""
    invoice_result = MagicMock()
    invoice_result.scalars.return_value.all.return_value = invoice_rows

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[business_result, invoice_result])

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return {"session_factory": factory}, session


def _trial_business(days_past_end):
    b = MagicMock()
    b.id = uuid.uuid4()
    b.billing_status = "TRIAL"
    b.is_active = True
    b.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=days_past_end)
    return b


async def test_no_expired_trials_or_overdue_invoices_does_nothing():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    ctx, session = _make_ctx(result, [])

    await sweep_trial_and_invoice_expiry(ctx)

    session.commit.assert_not_called()


async def test_trial_expired_within_grace_is_not_suspended():
    biz = _trial_business(days_past_end=settings.TRIAL_EXPIRY_GRACE_DAYS - 1)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [biz]
    ctx, session = _make_ctx(result, [])

    await sweep_trial_and_invoice_expiry(ctx)

    assert biz.is_active is True
    session.commit.assert_not_called()


async def test_trial_expired_past_grace_is_suspended_with_correct_reason():
    biz = _trial_business(days_past_end=settings.TRIAL_EXPIRY_GRACE_DAYS + 1)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [biz]
    ctx, session = _make_ctx(result, [])

    await sweep_trial_and_invoice_expiry(ctx)

    assert biz.is_active is False
    assert biz.suspended_reason == "TRIAL_EXPIRED"
    session.commit.assert_called_once()


async def test_overdue_invoices_are_marked_overdue():
    inv = MagicMock()
    inv.status = "AWAITING_PAYMENT"
    biz_result = MagicMock()
    biz_result.scalars.return_value.all.return_value = []
    ctx, session = _make_ctx(biz_result, [inv])

    await sweep_trial_and_invoice_expiry(ctx)

    assert inv.status == "OVERDUE"
    session.commit.assert_called_once()


def test_worker_settings_includes_the_new_cron_job():
    job_functions = [job.coroutine for job in WorkerSettings.cron_jobs]
    assert sweep_trial_and_invoice_expiry in job_functions
