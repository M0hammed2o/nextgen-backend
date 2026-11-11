"""
BUG-0191: unattended and lost delivery orders.

(a) staff are told about every decline and reminded about waiting orders;
(b) a RECEIVED order whose job was lost is re-enqueued;
(c) transient infrastructure failures are retried (ARQ needs `arq.Retry`).
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arq import Retry
from sqlalchemy.exc import OperationalError

from backend.app.delivery.order_service import (
    _publish_alert,
    delivery_accept_deadline_watchdog,
    delivery_order_recovery_and_reminders,
    process_delivery_order,
)
from shared.models.delivery import DeliveryConnection, DeliveryOrder
from tests.test_delivery_order_service import ScalarResult, TypedFakeDB, _connection, _delivery_order


class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.enqueued: list = []

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, seconds):
        return True

    async def enqueue_job(self, *args):
        self.enqueued.append(args)


def _ctx(db, arq_redis=None):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return {"session_factory": MagicMock(return_value=cm), "redis": arq_redis or FakeRedis(), "job_try": 1}


def _row(status, *, age_seconds, deadline_in=400, display_id="ABC12"):
    business_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    row = _delivery_order(business_id, uuid.uuid4(), status=status,
                          accept_deadline_at=now + timedelta(seconds=deadline_in))
    row.created_at = now - timedelta(seconds=age_seconds)
    row.display_id = display_id
    return row


async def _run_sweep(rows, redis=None, arq_redis=None):
    redis = redis or FakeRedis()
    arq_redis = arq_redis or FakeRedis()
    db = TypedFakeDB()
    db.register_select(DeliveryOrder, ScalarResult(rows=rows))
    with patch("backend.app.db.session.get_redis", new=AsyncMock(return_value=redis)), \
         patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()) as alert:
        await delivery_order_recovery_and_reminders(_ctx(db, arq_redis))
    return arq_redis, alert


class TestLostJobRecovery:
    async def test_stale_received_order_is_re_enqueued_once_per_interval(self):
        row = _row("RECEIVED", age_seconds=120)
        redis, arq_redis = FakeRedis(), FakeRedis()
        await _run_sweep([row], redis, arq_redis)
        await _run_sweep([row], redis, arq_redis)   # next tick, same interval
        assert arq_redis.enqueued == [
            ("process_delivery_order", "UBER_EATS", str(row.business_id), row.external_order_id, None)
        ]

    async def test_fresh_received_order_is_left_to_its_own_job(self):
        arq_redis, _ = await _run_sweep([_row("RECEIVED", age_seconds=10)])
        assert arq_redis.enqueued == []

    async def test_attempts_are_bounded(self):
        row = _row("RECEIVED", age_seconds=120)
        redis, arq_redis = FakeRedis(), FakeRedis()
        for _ in range(8):
            redis.store.pop(f"delivery:recovery:{row.id}", None)   # interval elapsed
            await _run_sweep([row], redis, arq_redis)
        assert len(arq_redis.enqueued) == 5

    async def test_sweep_never_accepts_or_denies(self):
        import inspect
        from backend.app.delivery import order_service
        source = inspect.getsource(order_service.delivery_order_recovery_and_reminders)
        assert "accept_order" not in source and "deny_order" not in source and "_transition" not in source


class TestReminders:
    async def test_waiting_order_reminds_at_90s_then_5min_once_each(self):
        row = _row("READY_TO_ACCEPT", age_seconds=100)
        redis = FakeRedis()
        _, alert = await _run_sweep([row], redis)
        assert [c.args[1] for c in alert.call_args_list] == ["delivery_order_waiting"]
        _, alert = await _run_sweep([row], redis)
        assert alert.call_count == 0                       # 90 s reminder already sent
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=320)
        _, alert = await _run_sweep([row], redis)
        assert alert.call_count == 1                       # the 5-minute one
        assert "min left" in alert.call_args.args[2].failure_reason

    async def test_needs_mapping_reminder_says_a_manager_must_map(self):
        _, alert = await _run_sweep([_row("NEEDS_MAPPING", age_seconds=95)])
        assert "manager must map" in alert.call_args.args[2].failure_reason

    async def test_no_reminder_before_90s(self):
        _, alert = await _run_sweep([_row("READY_TO_ACCEPT", age_seconds=30)])
        alert.assert_not_awaited()


async def test_watchdog_auto_deny_alerts_staff():
    business_id = uuid.uuid4()
    connection = _connection(business_id)
    overdue = _delivery_order(business_id, connection.id, status="READY_TO_ACCEPT",
                              accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=700))
    overdue.display_id = "XYZ99"
    db = TypedFakeDB()
    db.register_select(DeliveryOrder, ScalarResult(rows=[overdue]))
    db.register_get(DeliveryConnection, connection)
    adapter = MagicMock()
    adapter.deny_order = AsyncMock()
    with patch("backend.app.delivery.order_service.registry.get_provider", return_value=adapter), \
         patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()) as alert:
        await delivery_accept_deadline_watchdog(_ctx(db))
    alert.assert_awaited_once()
    assert alert.call_args.args[1] == "delivery_order_denied"
    assert alert.call_args.args[2].display_id == "XYZ99"
    assert "Nobody accepted it in time" in alert.call_args.args[2].failure_reason


async def test_decline_is_pushed_to_staff_phones():
    row = MagicMock(id=uuid.uuid4(), status="DENIED", provider="UBER_EATS", display_id="ABC12",
                    failure_reason="Nobody accepted it in time.", last_provider_sync_error=None)
    with patch("backend.app.db.session.get_redis", new=AsyncMock(return_value=MagicMock(publish=AsyncMock()))), \
         patch("backend.app.push_service.send_business_notification", new=AsyncMock()) as push:
        await _publish_alert(uuid.uuid4(), "delivery_order_denied", row, "ext-1")
    push.assert_awaited_once()
    assert push.call_args.kwargs["title"] == "Uber Eats order ABC12 was declined"


@pytest.mark.parametrize("error", [
    OperationalError("SELECT 1", {}, Exception("server closed the connection")),
    ConnectionError("redis down"),
])
async def test_infrastructure_failure_is_retried(error):
    session = AsyncMock()
    with patch("backend.app.delivery.order_service._handle_order", side_effect=error):
        with pytest.raises(Retry):
            await process_delivery_order(_ctx(session), "UBER_EATS", str(uuid.uuid4()), "order-1", None)
    session.rollback.assert_awaited_once()
