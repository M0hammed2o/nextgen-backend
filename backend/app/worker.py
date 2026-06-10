"""
ARQ Worker — async background job processing for NextGen Intelligence.

Replaces two previous background processing mechanisms:
  1. Synchronous webhook pipeline (was: called inline in the HTTP request cycle)
  2. Outbox worker asyncio task (was: asyncio.create_task in main.py lifespan)

All background processing now runs in a dedicated Render worker service:
  arq backend.app.worker.WorkerSettings

Jobs:
  process_whatsapp_message     — full bot pipeline for one inbound message

Cron jobs:
  run_outbox_batch             — every 5 seconds, sends PENDING outbox messages
  cancel_stale_delivery_fee    — every 10 minutes, cancels timed-out delivery orders

The bot pipeline (pipeline.py, state_machine.py, etc.) is NOT modified.
The worker wrapper provides the DB session; the pipeline handles everything else.
"""

import logging
from datetime import datetime, timedelta, timezone

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.bot.outbox_worker import _process_batch
from backend.app.bot.pipeline import process_inbound_message
from backend.app.bot.whatsapp_sender import send_notification_message
from backend.app.core.config import get_settings
from backend.app.db.session import DATABASE_URL
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.order import Order, OrderEvent

logger = logging.getLogger("nextgen.worker")

# How long a delivery order may sit in PENDING_DELIVERY_FEE before auto-cancel.
DELIVERY_FEE_TIMEOUT_MINUTES = 45


# ── Startup / Shutdown ────────────────────────────────────────────────────────

async def startup(ctx: dict) -> None:
    """
    Create a DB engine and session factory shared across all jobs in this worker
    process. Stored in ctx so each job function can create its own session.

    The worker uses a small pool (5 connections) separate from the API's pool,
    preventing the outbox worker from competing with request-handling connections.
    """
    engine = create_async_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "statement_cache_size": 0,
            "command_timeout": 30,
        },
    )
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    logger.info("ARQ worker started — DB pool ready.")


async def shutdown(ctx: dict) -> None:
    """Dispose the DB engine and close the pipeline's Redis pool on worker exit."""
    await ctx["engine"].dispose()
    try:
        from backend.app.db.session import close_redis
        await close_redis()
    except Exception:
        pass
    logger.info("ARQ worker stopped.")


# ── Job: WhatsApp message processing ─────────────────────────────────────────

async def process_whatsapp_message(
    ctx: dict,
    phone_number_id: str,
    wa_message_id: str,
    wa_id: str,
    msg_text: str,
    msg_type: str,
    raw_payload: dict,
    contact_name: str | None = None,
) -> None:
    """
    Process one inbound WhatsApp message through the full bot pipeline.

    Enqueued by the webhook handler after returning 200 to Meta.
    Creates its own DB session — no FastAPI dependency injection involved.

    The pipeline (process_inbound_message) handles its own commit/rollback,
    so this wrapper only needs to provide a session and close it afterwards.
    Infrastructure failures (DB temporarily unavailable) will cause the job to
    fail; ARQ will retry up to max_tries times. The pipeline's wa_message_id
    deduplication check ensures retries are safe and never double-process.
    """
    logger.info(
        "ARQ job start: wa_message_id=%s, phone_number_id=%s",
        wa_message_id,
        phone_number_id,
    )
    async with ctx["session_factory"]() as db:
        await process_inbound_message(
            db=db,
            phone_number_id=phone_number_id,
            wa_message_id=wa_message_id,
            wa_id=wa_id,
            msg_text=msg_text,
            msg_type=msg_type,
            raw_payload=raw_payload,
            contact_name=contact_name,
        )
    logger.info("ARQ job done: wa_message_id=%s", wa_message_id)


# ── Cron: Outbox delivery batch ───────────────────────────────────────────────

async def run_outbox_batch(ctx: dict) -> None:
    """
    Deliver PENDING messages from the message_outbox table.

    Runs every 5 seconds via ARQ cron. Replaces the outbox_worker asyncio
    background task that previously ran inside the API process.
    """
    async with ctx["session_factory"]() as db:
        try:
            processed = await _process_batch(db)
            if processed:
                logger.info("Outbox batch: sent %d message(s).", processed)
        except Exception:
            await db.rollback()
            logger.exception("Outbox batch failed — rolled back.")


# ── Cron: Delivery fee timeout ────────────────────────────────────────────────

async def cancel_stale_delivery_fee_orders(ctx: dict) -> None:
    """
    Cancel delivery orders stuck in PENDING_DELIVERY_FEE past the timeout.

    Runs every 10 minutes via ARQ cron.

    When a customer confirms a delivery order, it enters PENDING_DELIVERY_FEE
    so staff can set the fee. If staff do not act within DELIVERY_FEE_TIMEOUT_MINUTES,
    the order is cancelled here and the customer is notified via WhatsApp.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DELIVERY_FEE_TIMEOUT_MINUTES)

    # Collect notification data before committing so we can send outside the session.
    notifications: list[tuple[str, str, str]] = []  # (phone_number_id, wa_id, order_number)

    async with ctx["session_factory"]() as db:
        try:
            result = await db.execute(
                select(Order).where(
                    Order.status == "PENDING_DELIVERY_FEE",
                    Order.created_at < cutoff,
                )
            )
            stale = result.scalars().all()

            if not stale:
                return

            logger.warning(
                "Delivery fee timeout: cancelling %d order(s) older than %s min.",
                len(stale),
                DELIVERY_FEE_TIMEOUT_MINUTES,
            )

            for order in stale:
                order.status = "CANCELLED"
                order.cancelled_reason = (
                    f"Delivery fee not confirmed within {DELIVERY_FEE_TIMEOUT_MINUTES} minutes"
                )
                db.add(OrderEvent(
                    order_id=order.id,
                    business_id=order.business_id,
                    old_status="PENDING_DELIVERY_FEE",
                    new_status="CANCELLED",
                    reason="Automated timeout: delivery fee not set by staff",
                ))

                # Look up customer wa_id and business phone_number_id for notification.
                try:
                    if order.customer_id:
                        cust_row = await db.execute(
                            select(Customer.wa_id).where(Customer.id == order.customer_id)
                        )
                        wa_id = cust_row.scalar_one_or_none()

                        biz_row = await db.execute(
                            select(Business.whatsapp_phone_number_id).where(
                                Business.id == order.business_id
                            )
                        )
                        phone_number_id = biz_row.scalar_one_or_none()

                        if wa_id and phone_number_id:
                            notifications.append((phone_number_id, wa_id, order.order_number))
                except Exception:
                    logger.exception(
                        "Failed to collect notification data for order %s", order.id
                    )

            await db.commit()

        except Exception:
            await db.rollback()
            logger.exception("Delivery fee timeout job failed — rolled back.")
            return

    # Send notifications after the session is closed — fire-and-forget.
    # send_notification_message is a direct Meta API call with no DB dependency.
    for phone_number_id, wa_id, order_number in notifications:
        try:
            await send_notification_message(
                phone_number_id=phone_number_id,
                recipient_wa_id=wa_id,
                text=(
                    f"❌ Sorry, your order *{order_number}* has been cancelled.\n"
                    f"We were unable to confirm the delivery fee in time. "
                    f"Please place a new order or contact us directly. 🙏"
                ),
            )
            logger.info(
                "Timeout notification sent: order=%s, wa_id=%s", order_number, wa_id
            )
        except Exception:
            logger.exception(
                "Failed to send timeout notification for order %s", order_number
            )


# ── Worker Settings ───────────────────────────────────────────────────────────

_settings = get_settings()


class WorkerSettings:
    """
    ARQ worker configuration.

    Run with: arq backend.app.worker.WorkerSettings

    max_tries=3 applies to process_whatsapp_message jobs only.
    The pipeline never raises (it handles its own commit/rollback), so retries
    only fire on infrastructure failures (DB temporarily unavailable).
    wa_message_id deduplication in the pipeline makes retries safe.

    Cron jobs reschedule automatically regardless of success/failure.
    """

    functions = [process_whatsapp_message]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3

    cron_jobs = [
        # Deliver pending outbox messages — every 5 seconds.
        cron(
            run_outbox_batch,
            second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=False,
        ),
        # Cancel stale PENDING_DELIVERY_FEE orders — every 10 minutes.
        cron(
            cancel_stale_delivery_fee_orders,
            minute={0, 10, 20, 30, 40, 50},
            second=0,
            run_at_startup=True,
        ),
    ]

    redis_settings = RedisSettings.from_dsn(_settings.REDIS_URL)
