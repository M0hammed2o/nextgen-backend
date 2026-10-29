"""
ARQ worker unit tests.

Verifies the contracts that Phase 1 Track E depends on:

  1. process_whatsapp_message — wrapper calls pipeline with the correct args
     and passes the session from the factory (never a FastAPI-injected session).

  2. run_outbox_batch — delegates to _process_batch with the session from
     the factory. Does not call the pipeline directly.

  3. cancel_stale_delivery_fee_orders — cancels orders older than the timeout
     and skips orders within the timeout window.

  4. WorkerSettings structure — functions list, cron jobs, max_tries are
     wired up correctly so the ARQ worker can start without misconfiguration.

All tests are pure unit tests: no real DB, no real Redis, no network calls.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.app.worker import (
    DELIVERY_FEE_TIMEOUT_MINUTES,
    WorkerSettings,
    cancel_stale_delivery_fee_orders,
    process_whatsapp_message,
    run_outbox_batch,
)
from shared.models.business import Business


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(session: AsyncMock | None = None) -> dict:
    """
    Build a minimal ARQ ctx dict with a mock session factory.
    The factory returns the provided session (or a fresh AsyncMock) via
    an async context manager, mirroring what async_sessionmaker produces.
    """
    if session is None:
        session = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    return {"session_factory": factory}


# ── process_whatsapp_message ──────────────────────────────────────────────────

class TestProcessWhatsappMessage:

    async def test_calls_pipeline_with_correct_args(self):
        """
        The wrapper must pass every argument through to process_inbound_message
        unchanged, along with the DB session from the factory.
        """
        mock_db = AsyncMock()
        ctx = _make_ctx(mock_db)

        with patch(
            "backend.app.worker.process_inbound_message", new_callable=AsyncMock
        ) as mock_pipeline:
            await process_whatsapp_message(
                ctx,
                phone_number_id="123456789",
                wa_message_id="wamid.test_abc",
                wa_id="27831234567",
                msg_text="I want a burger please",
                msg_type="text",
                raw_payload={"id": "wamid.test_abc", "type": "text"},
                contact_name="Test Customer",
            )

        mock_pipeline.assert_called_once_with(
            db=mock_db,
            phone_number_id="123456789",
            wa_message_id="wamid.test_abc",
            wa_id="27831234567",
            msg_text="I want a burger please",
            msg_type="text",
            raw_payload={"id": "wamid.test_abc", "type": "text"},
            contact_name="Test Customer",
            # migration 0059 — always forwarded; None here since this call
            # didn't pass either, matching the pre-0059 fallback behaviour.
            expected_business_id=None,
            expected_routing_generation=None,
        )

    async def test_session_comes_from_factory_not_fastapi(self):
        """
        The session passed to the pipeline must be the one from ctx['session_factory'],
        confirming no FastAPI dependency injection is involved.
        """
        sentinel_db = AsyncMock(name="worker_db_session")
        ctx = _make_ctx(sentinel_db)

        captured_db = None

        async def capture_db(db, **kwargs):
            nonlocal captured_db
            captured_db = db

        with patch("backend.app.worker.process_inbound_message", side_effect=capture_db):
            await process_whatsapp_message(
                ctx,
                phone_number_id="p",
                wa_message_id="w",
                wa_id="v",
                msg_text="hi",
                msg_type="text",
                raw_payload={},
            )

        assert captured_db is sentinel_db

    async def test_contact_name_defaults_to_none(self):
        """contact_name is optional — omitting it must not raise."""
        ctx = _make_ctx()

        with patch(
            "backend.app.worker.process_inbound_message", new_callable=AsyncMock
        ) as mock_pipeline:
            await process_whatsapp_message(
                ctx,
                phone_number_id="p",
                wa_message_id="w",
                wa_id="v",
                msg_text="menu",
                msg_type="text",
                raw_payload={},
                # contact_name intentionally omitted
            )

        _, kwargs = mock_pipeline.call_args
        assert kwargs["contact_name"] is None


# ── run_outbox_batch ──────────────────────────────────────────────────────────

class TestRunOutboxBatch:

    async def test_delegates_to_process_batch(self):
        """
        run_outbox_batch must call _process_batch with the session from the
        factory and nothing else. It must not call the pipeline directly.
        """
        mock_db = AsyncMock()
        ctx = _make_ctx(mock_db)

        with patch(
            "backend.app.worker._process_batch", new_callable=AsyncMock, return_value=3
        ) as mock_batch:
            await run_outbox_batch(ctx)

        mock_batch.assert_called_once_with(mock_db)

    async def test_zero_messages_does_not_error(self):
        """_process_batch returning 0 (no pending messages) must not raise."""
        ctx = _make_ctx()

        with patch("backend.app.worker._process_batch", new_callable=AsyncMock, return_value=0):
            await run_outbox_batch(ctx)  # must not raise


# ── cancel_stale_delivery_fee_orders ─────────────────────────────────────────

class TestCancelStaleDeliveryFeeOrders:

    def _make_stale_order(self) -> MagicMock:
        """Fake Order in PENDING_DELIVERY_FEE older than the timeout."""
        order = MagicMock()
        order.id = uuid.uuid4()
        order.business_id = uuid.uuid4()
        order.customer_id = uuid.uuid4()
        order.order_number = "NG-001"
        order.status = "PENDING_DELIVERY_FEE"
        order.created_at = datetime.now(timezone.utc) - timedelta(
            minutes=DELIVERY_FEE_TIMEOUT_MINUTES + 10
        )
        return order

    async def test_no_stale_orders_does_nothing(self):
        """When the DB returns no stale orders the function returns without error."""
        mock_db = AsyncMock()

        # Worker now uses a JOIN query returning (Order, wa_id, phone_number_id) tuples.
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        ctx = _make_ctx(mock_db)
        await cancel_stale_delivery_fee_orders(ctx)

        mock_db.commit.assert_not_called()

    async def test_stale_order_is_cancelled(self):
        """An order past the timeout must have its status set to CANCELLED."""
        stale = self._make_stale_order()

        mock_db = AsyncMock()

        # Worker uses a single JOIN query returning
        # (Order, wa_id, phone_number_id, inventory_enabled).
        stale_result = MagicMock()
        stale_result.all.return_value = [(stale, None, None, False)]

        mock_db.execute = AsyncMock(return_value=stale_result)
        mock_db.add = MagicMock()

        ctx = _make_ctx(mock_db)

        with patch("backend.app.worker.send_notification_message", new_callable=AsyncMock):
            await cancel_stale_delivery_fee_orders(ctx)

        assert stale.status == "CANCELLED"
        assert "not confirmed" in stale.cancelled_reason
        mock_db.commit.assert_called_once()

    async def test_cancels_correct_number_of_orders(self):
        """Every stale order in the result set must be cancelled."""
        orders = [self._make_stale_order() for _ in range(3)]

        mock_db = AsyncMock()

        # Single JOIN query returns all rows at once.
        stale_result = MagicMock()
        stale_result.all.return_value = [(o, None, None, False) for o in orders]

        mock_db.execute = AsyncMock(return_value=stale_result)
        mock_db.add = MagicMock()

        ctx = _make_ctx(mock_db)

        with patch("backend.app.worker.send_notification_message", new_callable=AsyncMock):
            await cancel_stale_delivery_fee_orders(ctx)

        cancelled = [o for o in orders if o.status == "CANCELLED"]
        assert len(cancelled) == 3
        mock_db.commit.assert_called_once()

    async def test_notification_sent_when_wa_id_available(self):
        """
        When both customer wa_id and business phone_number_id are found in the
        JOIN query result, send_notification_message must be called correctly.
        """
        stale = self._make_stale_order()
        stale.order_number = "NG-042"

        mock_db = AsyncMock()

        # JOIN query returns wa_id and phone_number_id in the tuple.
        stale_result = MagicMock()
        stale_result.all.return_value = [(stale, "27831234567", "111222333", False)]

        mock_db.execute = AsyncMock(return_value=stale_result)
        mock_db.add = MagicMock()

        ctx = _make_ctx(mock_db)

        with patch(
            "backend.app.worker.send_notification_message", new_callable=AsyncMock
        ) as mock_notify:
            await cancel_stale_delivery_fee_orders(ctx)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args
        assert call_kwargs.kwargs["recipient_wa_id"] == "27831234567"
        assert call_kwargs.kwargs["phone_number_id"] == "111222333"
        assert "NG-042" in call_kwargs.kwargs["text"]
        # F6 fix (Wave 0A verifier): db + business must be passed so
        # send_notification_message resolves this business's own WhatsApp
        # connection (token_resolver.py) instead of always using the
        # platform token — see cancel_stale_delivery_fee_orders's own
        # short-lived-session-per-send comment.
        assert call_kwargs.kwargs["db"] is not None
        assert call_kwargs.kwargs["business"] is not None
        mock_db.get.assert_any_call(Business, stale.business_id)

    async def test_no_notification_when_wa_id_missing(self):
        """
        When the JOIN query returns no wa_id, send_notification_message must
        NOT be called — the cancellation still commits.
        """
        stale = self._make_stale_order()

        mock_db = AsyncMock()

        # wa_id=None → no notification sent
        stale_result = MagicMock()
        stale_result.all.return_value = [(stale, None, None, False)]

        mock_db.execute = AsyncMock(return_value=stale_result)
        mock_db.add = MagicMock()

        ctx = _make_ctx(mock_db)

        with patch(
            "backend.app.worker.send_notification_message", new_callable=AsyncMock
        ) as mock_notify:
            await cancel_stale_delivery_fee_orders(ctx)

        mock_notify.assert_not_called()
        mock_db.commit.assert_called_once()


# ── cancel_unpaid_orders ──────────────────────────────────────────────────────

class TestCancelUnpaidOrdersNotifications:
    """
    F6 fix (Wave 0A verifier): cancel_unpaid_orders' payment-timeout
    notification had the exact same gap as cancel_stale_delivery_fee_orders
    (no db/business passed) — same fix, same short-lived-session pattern.
    """

    def _make_unpaid_order(self) -> MagicMock:
        order = MagicMock()
        order.id = uuid.uuid4()
        order.business_id = uuid.uuid4()
        order.customer_id = uuid.uuid4()
        order.order_number = "NG-900"
        order.status = "NEW"
        order.payment_status = "UNPAID"
        order.created_at = datetime.now(timezone.utc) - timedelta(minutes=60)
        return order

    async def test_notification_passes_db_and_business(self):
        from backend.app.worker import DEFAULT_PAYMENT_TIMEOUT_MINUTES, cancel_unpaid_orders

        order = self._make_unpaid_order()
        mock_db = AsyncMock()

        rows_result = MagicMock()
        rows_result.all.return_value = [
            (order, "27831234567", "111222333", DEFAULT_PAYMENT_TIMEOUT_MINUTES, False)
        ]
        mock_db.execute = AsyncMock(return_value=rows_result)
        mock_db.add = MagicMock()

        ctx = _make_ctx(mock_db)

        with patch(
            "backend.app.worker.send_notification_message", new_callable=AsyncMock
        ) as mock_notify:
            await cancel_unpaid_orders(ctx)

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args
        assert call_kwargs.kwargs["recipient_wa_id"] == "27831234567"
        assert call_kwargs.kwargs["phone_number_id"] == "111222333"
        assert call_kwargs.kwargs["db"] is not None
        assert call_kwargs.kwargs["business"] is not None
        mock_db.get.assert_any_call(Business, order.business_id)


# ── WorkerSettings ────────────────────────────────────────────────────────────

class TestWorkerSettings:

    def test_process_whatsapp_message_registered(self):
        """The pipeline wrapper must be in the functions list."""
        names = [f.__name__ for f in WorkerSettings.functions]
        assert "process_whatsapp_message" in names

    def test_seven_cron_jobs_registered(self):
        """
        Outbox, delivery fee, payment timeout, billing grace-period
        suspension, (added for wave0a/sec — GET /ready worker-heartbeat
        check) the worker heartbeat, (added for the Wave 0A commercial/
        sales-onboarding push — trial-expiry + commercial-invoice-overdue
        sweep, see worker.py::sweep_trial_and_invoice_expiry's own docstring
        for why this is a separate cron job rather than folded into
        suspend_past_due_businesses), and (added in the gate hardening pass,
        2026-09-22 — WhatsApp Embedded Signup §2 failure-mode sweep) the
        stale-WhatsApp-connection observability sweep,
        log_stale_whatsapp_connections — and (added 2026-09-22, delivery
        marketplace module) the delivery accept-deadline watchdog,
        delivery_accept_deadline_watchdog (see
        backend/app/delivery/order_service.py's own docstring — auto-DENIES,
        never auto-accepts, anything past its accept/deny deadline) — and
        (added migration 0057_pos_entitlement, WhatsApp-only subscription
        package) the WhatsApp order-usage threshold-alert sweep,
        check_whatsapp_usage_alerts (monitoring/reporting only — reads
        orders/businesses, writes only its own new usage_alerts table,
        never touches the bot pipeline) — cron jobs must be registered.
        Count was 6, then 7, then 8, now 9; bumped deliberately each time,
        not a weakened assertion (this test still enumerates every job by
        name below).
        """
        assert len(WorkerSettings.cron_jobs) == 9
        names = {c.name for c in WorkerSettings.cron_jobs}
        assert "cron:run_outbox_batch" in names
        assert "cron:cancel_stale_delivery_fee_orders" in names
        assert "cron:cancel_unpaid_orders" in names
        assert "cron:suspend_past_due_businesses" in names
        assert "cron:sweep_trial_and_invoice_expiry" in names
        assert "cron:write_worker_heartbeat" in names
        assert "cron:log_stale_whatsapp_connections" in names
        assert "cron:delivery_accept_deadline_watchdog" in names
        assert "cron:check_whatsapp_usage_alerts" in names

    def test_max_tries_is_positive(self):
        """max_tries must be > 0 so infrastructure failures can be retried."""
        assert WorkerSettings.max_tries >= 1

    def test_delivery_fee_timeout_is_reasonable(self):
        """Timeout must be between 15 and 120 minutes — a sanity check."""
        assert 15 <= DELIVERY_FEE_TIMEOUT_MINUTES <= 120

    def test_startup_and_shutdown_are_set(self):
        """on_startup and on_shutdown must be wired so DB pool is managed."""
        assert WorkerSettings.on_startup is not None
        assert WorkerSettings.on_shutdown is not None
