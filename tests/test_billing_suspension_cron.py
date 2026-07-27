"""
backend/app/worker.py::suspend_past_due_businesses — the hourly cron job
that enforces the billing grace period (backend/app/billing/lifecycle.py).
Follows the exact AsyncMock/session_factory pattern already established in
test_arq_worker.py for this same worker module.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from backend.app.billing.lifecycle import GRACE_PERIOD_DAYS
from backend.app.worker import WorkerSettings, suspend_past_due_businesses


def _make_ctx(session=None):
    if session is None:
        session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return {"session_factory": factory}


def _make_business(days_past_due):
    b = MagicMock()
    b.id = uuid.uuid4()
    b.billing_status = "PAST_DUE"
    b.is_active = True
    b.billing_period_started_at = datetime.now(timezone.utc) - timedelta(days=days_past_due)
    return b


async def test_no_past_due_businesses_does_nothing():
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result)

    await suspend_past_due_businesses(_make_ctx(mock_db))

    mock_db.commit.assert_not_called()


async def test_business_within_grace_period_is_not_suspended():
    biz = _make_business(days_past_due=GRACE_PERIOD_DAYS - 1)
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [biz]
    mock_db.execute = AsyncMock(return_value=result)

    await suspend_past_due_businesses(_make_ctx(mock_db))

    assert biz.is_active is True
    mock_db.commit.assert_not_called()


async def test_business_past_grace_period_is_suspended():
    biz = _make_business(days_past_due=GRACE_PERIOD_DAYS + 1)
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [biz]
    mock_db.execute = AsyncMock(return_value=result)

    await suspend_past_due_businesses(_make_ctx(mock_db))

    assert biz.is_active is False
    assert biz.billing_status == "SUSPENDED"
    mock_db.commit.assert_called_once()


async def test_mixed_batch_only_suspends_the_ones_past_grace():
    within = _make_business(days_past_due=1)
    past = _make_business(days_past_due=GRACE_PERIOD_DAYS + 5)
    mock_db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [within, past]
    mock_db.execute = AsyncMock(return_value=result)

    await suspend_past_due_businesses(_make_ctx(mock_db))

    assert within.is_active is True
    assert past.is_active is False
    mock_db.commit.assert_called_once()


def test_worker_settings_includes_the_new_cron_job():
    job_functions = [job.coroutine for job in WorkerSettings.cron_jobs]
    assert suspend_past_due_businesses in job_functions
