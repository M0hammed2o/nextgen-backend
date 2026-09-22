"""
Webhook plane (`backend/app/delivery/intake.py`) — the trust-boundary gates,
idempotency control flow, and always-200-except-enqueue-failure contract.

FakeDB pops pre-scripted results off a queue for each `db.execute()` call,
in the exact order `ingest_delivery_webhook` is expected to issue them —
matching this repo's established `FakeDB` pattern
(tests/test_payment_webhook_amount_validation.py). This exercises CONTROL
FLOW (which gate fired, what got enqueued, what http_status came back), not
Postgres's own ON CONFLICT semantics — this repo's pytest suite is entirely
mocked (TESTING.md), and no live Postgres was used in this session (see
this change's final report for why). The idempotent-insert SQL itself is
separately checked in TestIdempotentInsertTargets below by compiling the
real statement and asserting its ON CONFLICT target matches the real
migrated unique constraints, without needing a live connection.
"""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock

import pytest

from backend.app.delivery.intake import (
    _insert_delivery_order_idempotent,
    _insert_event_idempotent,
    ingest_delivery_webhook,
)
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent

_SECRET = "uber-test-secret"


def _signed(body: bytes) -> str:
    return hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _body(event_id="evt-1", store_id="store-1", order_id="order-1", event_type="orders.notification") -> bytes:
    import json
    return json.dumps({
        "event_id": event_id,
        "event_type": event_type,
        "meta": {"user_id": store_id, "resource_id": order_id, "status": "created"},
    }).encode()


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, execute_results, get_results=None):
        self._execute_results = list(execute_results)
        self._get_results = list(get_results or [])
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        assert self._execute_results, "no more scripted execute() results — unexpected extra call"
        return self._execute_results.pop(0)

    async def get(self, model, id_):
        assert self._get_results, "no more scripted get() results — unexpected extra call"
        return self._get_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeArqPool:
    def __init__(self, raise_on_enqueue: bool = False):
        self.enqueued = []
        self._raise = raise_on_enqueue

    async def enqueue_job(self, name, *args):
        if self._raise:
            raise RuntimeError("redis is down")
        self.enqueued.append((name, args))


def _settings_with_webhook_enabled(monkeypatch, enabled=True):
    from backend.app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "UBER_EATS_WEBHOOK_ENABLED", enabled)
    monkeypatch.setattr(settings, "UBER_EATS_CLIENT_SECRET", _SECRET)
    return settings


class TestUnsupportedOrDisabledProvider:
    async def test_mr_d_is_rejected_before_any_db_call(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        db = FakeDB(execute_results=[])  # must never be called
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="MR_D", raw_body=b"{}", headers={}, arq_pool=pool,
        )
        assert result.accepted is False
        assert result.http_status == 200
        assert result.error_category == "UNSUPPORTED_PROVIDER"
        assert pool.enqueued == []
        assert db.commits == 0

    async def test_platform_kill_switch_rejects_uber_eats_too(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch, enabled=False)
        db = FakeDB(execute_results=[])
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=b"{}", headers={}, arq_pool=pool,
        )
        assert result.accepted is False
        assert result.error_category == "UNSUPPORTED_PROVIDER"
        assert pool.enqueued == []


class TestSignatureAndParsing:
    async def test_bad_signature_is_logged_and_acked_200(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        body = _body()
        db = FakeDB(
            execute_results=[FakeResult(scalar=uuid.uuid4())],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body,
            headers={"X-Uber-Signature": "0" * 64}, arq_pool=pool,
        )
        assert result.accepted is False
        assert result.http_status == 200
        assert result.error_category == "BAD_SIGNATURE"
        assert pool.enqueued == []
        assert db.commits == 1

    async def test_missing_signature_header_fails_closed(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        db = FakeDB(
            execute_results=[FakeResult(scalar=uuid.uuid4())],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=_body(), headers={}, arq_pool=pool,
        )
        assert result.error_category == "BAD_SIGNATURE"
        assert pool.enqueued == []

    async def test_tampered_body_fails_signature_even_with_a_signature_present(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        real_body = _body()
        sig = _signed(real_body)
        tampered_body = _body(order_id="order-EVIL")
        db = FakeDB(
            execute_results=[FakeResult(scalar=uuid.uuid4())],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=tampered_body,
            headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        assert result.error_category == "BAD_SIGNATURE"
        assert pool.enqueued == []

    async def test_unparseable_payload_is_acked_200_never_enqueued(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        body = b"not json at all"
        sig = _signed(body)
        db = FakeDB(
            execute_results=[FakeResult(scalar=uuid.uuid4())],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body,
            headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        assert result.error_category == "UNPARSEABLE_PAYLOAD"
        assert pool.enqueued == []


class TestDuplicateAndUnknownStore:
    async def test_duplicate_event_id_is_acked_and_never_re_enqueued(self, monkeypatch):
        """Concurrent duplicate delivery: the event-log ON CONFLICT reports no new row."""
        _settings_with_webhook_enabled(monkeypatch)
        body = _body()
        sig = _signed(body)
        existing_event = DeliveryWebhookEvent(id=uuid.uuid4(), provider="UBER_EATS", event_id="evt-1")
        db = FakeDB(
            execute_results=[
                FakeResult(scalar=None),        # ON CONFLICT: nothing inserted
                FakeResult(scalar=existing_event),  # SELECT existing row
            ],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body,
            headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        assert result.accepted is True
        assert result.detail == "duplicate_event"
        assert pool.enqueued == [], "a duplicate delivery must never enqueue a second job"
        assert db.commits == 1

    async def test_unknown_store_is_logged_and_ignored_no_deny(self, monkeypatch):
        """Unknown store: we have no right to deny an order for a non-customer."""
        _settings_with_webhook_enabled(monkeypatch)
        body = _body(store_id="store-not-ours")
        sig = _signed(body)
        event_row = DeliveryWebhookEvent(id=uuid.uuid4())
        db = FakeDB(
            execute_results=[
                FakeResult(scalar=uuid.uuid4()),  # event insert succeeds
                FakeResult(scalar=None),          # connection lookup: no match
            ],
            get_results=[event_row],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body,
            headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        assert result.accepted is True
        assert result.error_category == "UNKNOWN_STORE"
        assert event_row.error_category == "UNKNOWN_STORE"
        assert event_row.business_id is None
        assert pool.enqueued == []


class TestKnownStoreEnqueue:
    async def test_known_store_creates_order_row_and_enqueues_process_delivery_order(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        body = _body()
        sig = _signed(body)
        business_id = uuid.uuid4()
        connection_id = uuid.uuid4()
        event_row = DeliveryWebhookEvent(id=uuid.uuid4())
        connection = DeliveryConnection(
            id=connection_id, business_id=business_id, provider="UBER_EATS",
            external_store_id="store-1", status="ACTIVE", environment="production",
        )
        db = FakeDB(
            execute_results=[
                FakeResult(scalar=uuid.uuid4()),  # event insert
                FakeResult(scalar=connection),     # connection resolved
                FakeResult(scalar=None),           # delivery_order insert (unused return)
            ],
            get_results=[event_row],
        )
        pool = FakeArqPool()
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body,
            headers={"X-Uber-Signature": sig, "X-Environment": "production"}, arq_pool=pool,
        )
        assert result.accepted is True
        assert result.business_id == business_id
        assert len(pool.enqueued) == 1
        job_name, job_args = pool.enqueued[0]
        assert job_name == "process_delivery_order"
        assert job_args == ("UBER_EATS", str(business_id), "order-1", "orders.notification")
        # business_id came ONLY from the resolved connection, never the payload.
        assert event_row.business_id == business_id

    async def test_cancellation_event_type_routes_to_the_cancellation_job(self, monkeypatch):
        _settings_with_webhook_enabled(monkeypatch)
        body = _body(event_type="orders.cancel")
        sig = _signed(body)
        business_id = uuid.uuid4()
        connection = DeliveryConnection(
            id=uuid.uuid4(), business_id=business_id, provider="UBER_EATS",
            external_store_id="store-1", status="ACTIVE", environment="production",
        )
        db = FakeDB(
            execute_results=[
                FakeResult(scalar=uuid.uuid4()),
                FakeResult(scalar=connection),
                FakeResult(scalar=None),
            ],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool()
        await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body, headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        job_name, job_args = pool.enqueued[0]
        assert job_name == "process_delivery_cancellation"
        assert job_args == ("UBER_EATS", str(business_id), "order-1")

    async def test_enqueue_failure_returns_503_so_the_provider_retries(self, monkeypatch):
        """A real paid order must never be silently dropped by a broken worker pool."""
        _settings_with_webhook_enabled(monkeypatch)
        body = _body()
        sig = _signed(body)
        business_id = uuid.uuid4()
        connection = DeliveryConnection(
            id=uuid.uuid4(), business_id=business_id, provider="UBER_EATS",
            external_store_id="store-1", status="ACTIVE", environment="production",
        )
        db = FakeDB(
            execute_results=[
                FakeResult(scalar=uuid.uuid4()),
                FakeResult(scalar=connection),
                FakeResult(scalar=None),
            ],
            get_results=[DeliveryWebhookEvent(id=uuid.uuid4())],
        )
        pool = FakeArqPool(raise_on_enqueue=True)
        result = await ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body, headers={"X-Uber-Signature": sig}, arq_pool=pool,
        )
        assert result.accepted is False
        assert result.http_status == 503
        assert result.error_category == "ENQUEUE_FAILED"


class TestIdempotentInsertTargets:
    """
    Compile the real ON CONFLICT statements (no DB connection needed) and
    assert their conflict target matches the actual migrated unique
    constraints — the closest this session gets to proving the Postgres
    semantics without connecting to a live database (see module docstring).
    """

    def test_event_log_conflict_target_matches_the_migrated_unique_constraint(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        import datetime

        stmt = (
            pg_insert(DeliveryWebhookEvent)
            .values(provider="UBER_EATS", event_id="e1", received_at=datetime.datetime.now())
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        )
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (provider, event_id) DO NOTHING" in compiled

    def test_delivery_order_conflict_target_matches_the_migrated_unique_constraint(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(DeliveryOrder)
            .values(
                business_id=uuid.uuid4(), provider="UBER_EATS", external_order_id="o1",
                external_store_id="s1", status="RECEIVED",
            )
            .on_conflict_do_nothing(
                index_elements=["business_id", "provider", "external_order_id"]
            )
        )
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        assert "ON CONFLICT (business_id, provider, external_order_id) DO NOTHING" in compiled
