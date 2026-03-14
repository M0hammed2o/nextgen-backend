"""
Outbox Worker — background task for reliable message delivery.

Model 1: Uses platform token from env (WHATSAPP_DEFAULT_ACCESS_TOKEN).
No per-business token lookup needed.

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

from backend.app.bot.whatsapp_sender import send_whatsapp_message
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
    )
    messages = list(result.scalars().all())

    if not messages:
        return 0

    for msg in messages:
        await _process_single(db, msg)

    await db.commit()
    return len(messages)


async def _process_single(db: AsyncSession, msg: MessageOutbox) -> None:
    """Attempt to send a single outbox message."""
    # Load business to get phone_number_id (routing only — no token)
    result = await db.execute(
        select(Business).where(Business.id == msg.business_id)
    )
    business = result.scalar_one_or_none()

    if not business or not business.whatsapp_phone_number_id:
        msg.status = "FAILED"
        msg.last_error = "Business not found or missing phone_number_id"
        return

    if not business.is_whatsapp_enabled:
        msg.status = "FAILED"
        msg.last_error = "WhatsApp disabled for this business"
        return

    msg.attempts += 1
    msg.status = "SENDING"
    await db.flush()

    # Attempt send — platform token from env, NOT per-business
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": msg.customer_wa_id,
        "type": "text",
        "text": {"body": msg.message_text},
    }

    send_result = await send_whatsapp_message(
        phone_number_id=business.whatsapp_phone_number_id,
        payload=payload,
    )

    if send_result["success"]:
        msg.status = "SENT"
        msg.sent_wa_message_id = send_result.get("wa_message_id")
        msg.sent_at = datetime.now(timezone.utc)
    else:
        if msg.attempts >= msg.max_attempts:
            msg.status = "FAILED"
            msg.last_error = f"Max attempts ({msg.max_attempts}) reached. Last: {send_result.get('error', 'unknown')[:200]}"
        else:
            msg.status = "PENDING"
            # Exponential backoff: 5s, 25s, 125s
            delay = POLL_INTERVAL_SECONDS * (5 ** (msg.attempts - 1))
            msg.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            msg.last_error = send_result.get("error", "unknown")[:500]


# ── Entry point for standalone worker ────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    asyncio.run(run_outbox_worker())
