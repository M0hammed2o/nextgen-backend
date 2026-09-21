"""
Outbox Worker — background task for reliable message delivery.

Model 1: Uses platform token from env (WHATSAPP_DEFAULT_ACCESS_TOKEN) for
any business with no active customer-owned WhatsApp connection.

Wave 0A / Milestone C / DECISION-0022: for a business with a CONNECTED
whatsapp_connections row, sends resolve to that business's OWN token
instead (backend/app/whatsapp/token_resolver.py via
_send_via_business_connection) — with NO fallback to the platform token on
a Graph error 190 (the platform token has no rights on a customer-owned
WABA). A retry that finds no usable credentials (e.g. connection is
NEEDS_ATTENTION) fails this attempt and is retried later by the existing
backoff below, same as any other send failure — it does not raise or skip
the batch.

Picks up PENDING messages from message_outbox, attempts to send via Meta API,
and marks as SENT or FAILED (up to max_attempts).

Run as: python -m backend.app.bot.outbox_worker
Or integrate into app lifespan as a background task.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.bot.whatsapp_sender import _send_via_business_connection
from backend.app.core.config import get_settings
from shared.models.audit import MessageOutbox
from shared.models.business import Business

logger = logging.getLogger("nextgen.outbox")
settings = get_settings()

POLL_INTERVAL_SECONDS = 5
BATCH_SIZE = 20


async def run_outbox_worker():
    """
    Main loop: poll for PENDING messages, attempt to send, update status.
    Runs indefinitely until cancelled.
    """
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    logger.info("Outbox worker started (poll interval: %ds)", POLL_INTERVAL_SECONDS)

    try:
        while True:
            try:
                async with session_factory() as db:
                    processed = await _process_batch(db)
                    if processed > 0:
                        logger.info("Outbox worker processed %d messages", processed)
            except Exception:
                logger.exception("Outbox worker error")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await engine.dispose()
        logger.info("Outbox worker stopped")


async def _process_batch(db: AsyncSession) -> int:
    """Process a batch of pending outbox messages."""
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(MessageOutbox)
        .where(
            MessageOutbox.status == "PENDING",
            MessageOutbox.scheduled_at <= now,
            MessageOutbox.attempts < MessageOutbox.max_attempts,
        )
        .order_by(MessageOutbox.scheduled_at)
        .limit(BATCH_SIZE)
        .with_for_update(skip_locked=True)  # Safe concurrent workers — skip rows locked by another batch
    )
    messages = list(result.scalars().all())

    if not messages:
        return 0

    for msg in messages:
        await _process_single(db, msg)

    await db.commit()
    return len(messages)


async def _process_single(db: AsyncSession, msg: MessageOutbox) -> None:
    """Attempt to send a single outbox message using platform token."""
    # Load business for phone_number_id
    result = await db.execute(
        select(Business).where(Business.id == msg.business_id)
    )
    business = result.scalar_one_or_none()

    if not business or not business.whatsapp_phone_number_id:
        msg.status = "FAILED"
        msg.last_error = "Business not found or missing phone_number_id"
        return

    if not getattr(business, "is_whatsapp_enabled", True):
        msg.status = "FAILED"
        msg.last_error = "WhatsApp disabled for this business"
        return

    msg.attempts += 1
    msg.status = "SENDING"
    await db.flush()

    # Resolve per-business credentials (Wave 0A / Milestone C) — falls back
    # to the platform token internally when this business has no active
    # customer-owned connection; NO fallback when a connection exists but
    # isn't CONNECTED (see _send_via_business_connection docstring).
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": msg.customer_wa_id,
        "type": "text",
        "text": {"body": msg.message_text},
    }

    wa_message_id, error_detail = await _send_via_business_connection(db, business, payload)

    if wa_message_id:
        msg.status = "SENT"
        msg.sent_wa_message_id = wa_message_id
        msg.sent_at = datetime.now(timezone.utc)
    else:
        if msg.attempts >= msg.max_attempts:
            msg.status = "FAILED"
            msg.last_error = error_detail or f"Max attempts ({msg.max_attempts}) reached"
        else:
            msg.status = "PENDING"
            # Exponential backoff: 5s, 25s, 125s
            delay = POLL_INTERVAL_SECONDS * (5 ** (msg.attempts - 1))
            msg.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            msg.last_error = error_detail or "Send failed, retrying"


# ── Entry point for standalone worker ────────────────────────────────────────
# DEPRECATED: In production, outbox delivery is handled by the ARQ cron job
# `run_outbox_batch` in backend/app/worker.py.
# Running this standalone AND the ARQ worker simultaneously will cause two
# workers to compete for the same outbox messages.  Only run this standalone
# entry point for local development when the ARQ worker is not running.

if __name__ == "__main__":
    import warnings
    warnings.warn(
        "Running outbox_worker.py standalone is intended for local development only. "
        "In production, outbox delivery is managed by the ARQ worker cron (run_outbox_batch). "
        "Do NOT run both simultaneously.",
        DeprecationWarning,
        stacklevel=1,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    asyncio.run(run_outbox_worker())
