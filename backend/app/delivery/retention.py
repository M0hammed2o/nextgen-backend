"""
Two-stage retention for `delivery_webhook_events` (design doc §3 — the first
table on this platform with retention defined): the raw envelope is redacted
after `DELIVERY_WEBHOOK_EVENT_REDACT_AFTER_DAYS`, and the row itself deleted
after `DELIVERY_WEBHOOK_EVENT_DELETE_AFTER_DAYS`. The settings and the
`payload_redacted_at` column existed since migration 0041, but nothing ever
ran them (TODO.md) — every raw payload sat forever.

Deleting an event never loses an order: `delivery_orders.raw_event_id` is
`ON DELETE SET NULL`, and the order row carries everything reconciliation
needs. Batched so one run never holds a long lock on a busy table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from backend.app.core.config import get_settings
from shared.models.delivery import DeliveryWebhookEvent

logger = logging.getLogger("nextgen.delivery.retention")

_BATCH = 1000


async def delivery_webhook_retention_sweep(ctx: dict) -> None:
    """ARQ cron. Redacts then deletes aged webhook events, one bounded batch each."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    redact_before = now - timedelta(days=settings.DELIVERY_WEBHOOK_EVENT_REDACT_AFTER_DAYS)
    delete_before = now - timedelta(days=settings.DELIVERY_WEBHOOK_EVENT_DELETE_AFTER_DAYS)

    async with ctx["session_factory"]() as db:
        try:
            redact_ids = select(DeliveryWebhookEvent.id).where(
                DeliveryWebhookEvent.received_at < redact_before,
                DeliveryWebhookEvent.payload_redacted_at.is_(None),
            ).limit(_BATCH)
            redacted = await db.execute(
                update(DeliveryWebhookEvent)
                .where(DeliveryWebhookEvent.id.in_(redact_ids))
                .values(raw_payload_json=None, payload_redacted_at=now)
                .execution_options(synchronize_session=False)
            )
            delete_ids = select(DeliveryWebhookEvent.id).where(
                DeliveryWebhookEvent.received_at < delete_before,
            ).limit(_BATCH)
            deleted = await db.execute(
                delete(DeliveryWebhookEvent)
                .where(DeliveryWebhookEvent.id.in_(delete_ids))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if redacted.rowcount or deleted.rowcount:
                logger.info(
                    "DELIVERY_WEBHOOK_RETENTION: redacted=%s deleted=%s",
                    redacted.rowcount, deleted.rowcount,
                )
        except Exception:
            await db.rollback()
            logger.exception("DELIVERY_WEBHOOK_RETENTION_FAILED")
