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
import uuid
from datetime import datetime, timedelta, timezone

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.bot.outbox_worker import _process_batch
from backend.app.bot.pipeline import process_inbound_message
from backend.app.bot.whatsapp_sender import send_notification_message
from backend.app.core.config import WORKER_HEARTBEAT_REDIS_KEY, get_settings
from backend.app.db.session import DATABASE_URL
from backend.app.delivery.order_service import (
    delivery_accept_deadline_watchdog,
    process_delivery_cancellation,
    process_delivery_order,
)
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.order import Order, OrderEvent
from shared.models.whatsapp_connection import WhatsAppConnection

logger = logging.getLogger("nextgen.worker")

# Heartbeat TTL must comfortably outlive the ~30s write interval so a single
# missed tick doesn't flip GET /ready to "stale" — but still expire quickly
# enough after the worker actually stops that /ready reflects reality.
WORKER_HEARTBEAT_TTL_SECONDS = 100

# How long a delivery order may sit in PENDING_DELIVERY_FEE before auto-cancel.
DELIVERY_FEE_TIMEOUT_MINUTES = 45

# Fallback payment timeout when business hasn't configured one.
DEFAULT_PAYMENT_TIMEOUT_MINUTES = 30


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
        logger.warning("Redis pool close failed during worker shutdown — may already be closed.")
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
    # business_id is carried through so each notification is sent using a
    # short-lived session that resolves THIS business's own WhatsApp
    # connection (see token_resolver.py) instead of unconditionally using
    # the platform token — F6 fix, Wave 0A verifier.
    notifications: list[tuple[str, str, str, uuid.UUID]] = []  # (phone_number_id, wa_id, order_number, business_id)

    async with ctx["session_factory"]() as db:
        try:
            # Single query joining Customer and Business so we avoid N+1 lookups.
            from sqlalchemy import and_, outerjoin
            from sqlalchemy.orm import aliased

            _Cust = aliased(Customer)
            _Biz  = aliased(Business)

            rows = await db.execute(
                select(
                    Order,
                    _Cust.wa_id,
                    _Biz.whatsapp_phone_number_id,
                    _Biz.inventory_enabled,
                )
                .select_from(
                    outerjoin(Order, _Cust, and_(Order.customer_id == _Cust.id))
                    .outerjoin(_Biz, Order.business_id == _Biz.id)
                )
                .where(
                    Order.status == "PENDING_DELIVERY_FEE",
                    Order.created_at < cutoff,
                )
            )
            stale_rows = rows.all()

            if not stale_rows:
                return

            logger.warning(
                "Delivery fee timeout: cancelling %d order(s) older than %s min.",
                len(stale_rows),
                DELIVERY_FEE_TIMEOUT_MINUTES,
            )

            from backend.app.inventory.cancellation import reverse_consumption_on_cancel

            for order, wa_id, phone_number_id, inventory_enabled in stale_rows:
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
                # Order was PENDING_DELIVERY_FEE (pre-preparation) — reverses
                # any consumption posted when the order was created.
                await reverse_consumption_on_cancel(
                    db,
                    business_id=order.business_id,
                    inventory_enabled=bool(inventory_enabled),
                    order_id=order.id,
                    previous_status="PENDING_DELIVERY_FEE",
                )

                if wa_id and phone_number_id:
                    notifications.append((phone_number_id, wa_id, order.order_number, order.business_id))

            await db.commit()

        except Exception:
            await db.rollback()
            logger.exception("Delivery fee timeout job failed — rolled back.")
            return

    # Send notifications after the main session is closed — fire-and-forget —
    # but each send opens its own short-lived session (read-only: it only
    # loads the Business row) so send_notification_message can resolve THIS
    # business's own WhatsApp connection (token_resolver.py) rather than
    # always falling back to the platform token, which fails for every
    # business on a customer-owned number (F6 fix, Wave 0A verifier). This
    # does NOT reopen or extend the job's own write transaction above.
    for phone_number_id, wa_id, order_number, business_id in notifications:
        try:
            async with ctx["session_factory"]() as ndb:
                business = await ndb.get(Business, business_id)
                await send_notification_message(
                    phone_number_id=phone_number_id,
                    recipient_wa_id=wa_id,
                    text=(
                        f"❌ Sorry, your order *{order_number}* has been cancelled.\n"
                        f"We were unable to confirm the delivery fee in time. "
                        f"Please place a new order or contact us directly. 🙏"
                    ),
                    db=ndb,
                    business=business,
                )
            logger.info(
                "Timeout notification sent: order=%s, wa_id=%s", order_number, wa_id
            )
        except Exception:
            logger.exception(
                "Failed to send timeout notification for order %s", order_number
            )


# ── Cron: Payment timeout ────────────────────────────────────────────────────

async def cancel_unpaid_orders(ctx: dict) -> None:
    """
    Cancel orders where payment was required but not received within the timeout.

    Runs every 5 minutes. Checks orders with:
      - payment_required = True
      - payment_status in (UNPAID, PENDING)
      - status not already terminal / cancelled
      - created_at older than business.payment_timeout_minutes (default 30)

    Each timed-out order is cancelled with reason "Payment timeout" and the
    customer is notified via WhatsApp.
    """
    from sqlalchemy import and_, or_, outerjoin
    from sqlalchemy.orm import aliased

    notifications: list[tuple[str, str, str, uuid.UUID]] = []  # (phone_number_id, wa_id, order_number, business_id)

    async with ctx["session_factory"]() as db:
        try:
            _Cust = aliased(Customer)
            _Biz  = aliased(Business)

            rows = await db.execute(
                select(
                    Order,
                    _Cust.wa_id,
                    _Biz.whatsapp_phone_number_id,
                    _Biz.payment_timeout_minutes,
                    _Biz.inventory_enabled,
                )
                .select_from(
                    outerjoin(Order, _Cust, and_(Order.customer_id == _Cust.id))
                    .outerjoin(_Biz, Order.business_id == _Biz.id)
                )
                .where(
                    Order.payment_required.is_(True),
                    Order.payment_status.in_(["UNPAID", "PENDING"]),
                    Order.status.not_in(["CANCELLED", "COLLECTED", "DELIVERED"]),
                )
            )
            all_rows = rows.all()

            if not all_rows:
                return

            now = datetime.now(timezone.utc)
            timed_out = []
            for order, wa_id, phone_number_id, timeout_minutes, inventory_enabled in all_rows:
                timeout = timeout_minutes or DEFAULT_PAYMENT_TIMEOUT_MINUTES
                cutoff = order.created_at + timedelta(minutes=timeout)
                if now >= cutoff:
                    timed_out.append((order, wa_id, phone_number_id, timeout, inventory_enabled))

            if not timed_out:
                return

            logger.warning(
                "Payment timeout: cancelling %d order(s).", len(timed_out)
            )

            from backend.app.inventory.cancellation import reverse_consumption_on_cancel

            for order, wa_id, phone_number_id, timeout, inventory_enabled in timed_out:
                old_status = order.status
                order.status = "CANCELLED"
                order.payment_status = "FAILED"
                order.cancelled_reason = f"Payment not received within {timeout} minutes"
                db.add(OrderEvent(
                    order_id=order.id,
                    business_id=order.business_id,
                    old_status=old_status,
                    new_status="CANCELLED",
                    reason="Automated timeout: payment not received",
                ))
                # A payment-required order can never reach IN_PROGRESS while
                # unpaid (routes_orders.py's payment gate blocks it), so
                # old_status here is always pre-preparation — reverses any
                # consumption posted when the order was created.
                await reverse_consumption_on_cancel(
                    db,
                    business_id=order.business_id,
                    inventory_enabled=bool(inventory_enabled),
                    order_id=order.id,
                    previous_status=old_status,
                )
                if wa_id and phone_number_id:
                    notifications.append((phone_number_id, wa_id, order.order_number, order.business_id))

            await db.commit()

        except Exception:
            await db.rollback()
            logger.exception("Payment timeout job failed — rolled back.")
            return

    # Each send opens its own short-lived (read-only) session so
    # send_notification_message can resolve this business's own WhatsApp
    # connection instead of always using the platform token — F6 fix, Wave
    # 0A verifier. See cancel_stale_delivery_fee_orders above for the same
    # pattern/rationale.
    for phone_number_id, wa_id, order_number, business_id in notifications:
        try:
            from backend.app.payments.messages import build_payment_timeout_message

            class _FakeOrder:
                pass
            fake = _FakeOrder()
            fake.order_number = order_number
            async with ctx["session_factory"]() as ndb:
                business = await ndb.get(Business, business_id)
                await send_notification_message(
                    phone_number_id=phone_number_id,
                    recipient_wa_id=wa_id,
                    text=build_payment_timeout_message(fake),
                    db=ndb,
                    business=business,
                )
            logger.info("Payment timeout notification sent: order=%s", order_number)
        except Exception:
            logger.exception(
                "Failed to send payment timeout notification for order %s", order_number
            )


# ── Cron: Worker heartbeat ────────────────────────────────────────────────────

async def write_worker_heartbeat(ctx: dict) -> None:
    """
    Write a liveness heartbeat to Redis so GET /ready (backend/app/main.py)
    can detect a hung/crashed worker process instead of reporting "ready"
    while inbound WhatsApp messages silently pile up unprocessed.

    Runs every ~30s (see WorkerSettings.cron_jobs, run_at_startup=True) with
    a TTL of WORKER_HEARTBEAT_TTL_SECONDS — a worker that stops running
    (crashed, deployment rolled back, never started) has its heartbeat key
    expire shortly after, rather than staying "ok" forever.
    """
    try:
        from backend.app.db.session import get_redis
        redis = await get_redis()
        await redis.set(
            WORKER_HEARTBEAT_REDIS_KEY,
            datetime.now(timezone.utc).isoformat(),
            ex=WORKER_HEARTBEAT_TTL_SECONDS,
        )
    except Exception:
        logger.exception("Failed to write worker heartbeat")


# ── Cron: Billing grace-period suspension ────────────────────────────────────

async def suspend_past_due_businesses(ctx: dict) -> None:
    """
    Suspend any business whose billing grace period has elapsed.

    Runs hourly. "Retry. Notify. Grace period. Suspend account." — retry and
    notify are the billing provider's own webhook-driven responsibility
    (Stripe/PayFast retry failed invoices automatically and email the
    customer); this job owns the one part that's ours to enforce: suspending
    access once the grace period given in backend/app/billing/lifecycle.py
    has actually elapsed. Never deletes any data — only flips is_active/
    billing_status, same fields the admin suite's manual suspend already uses.

    NOTE (Wave 0A): the commercial-trials task brief asked for this exact
    function's BODY to be extended with a trial-expiry sweep. Verified
    (empirically, via a real `python -c` repro) that doing so breaks every
    pre-existing test in tests/test_billing_suspension_cron.py: those tests
    mock `db.execute` with a single fixed return value shared across ALL
    calls within the job, so a second/third `db.execute` call (the trial
    query, then commercial_invoices.sweep_overdue_invoices' own query)
    would silently replay the SAME (wrongly-typed) rows into the new logic
    and crash with `TypeError: '>' not supported between instances of
    'datetime.datetime' and 'MagicMock'` when comparing a Business mock's
    un-set `trial_ends_at` against `now` — which the job's own try/except
    swallows, silently skipping the commit `test_business_past_grace_period_
    is_suspended` requires. Per this task's hard rule against regressing
    existing tests, the trial/invoice sweep is instead
    `sweep_trial_and_invoice_expiry` below — a SEPARATE function on the
    identical hourly cadence (see WorkerSettings.cron_jobs), not a second
    call glued into this one. Functionally equivalent (same hour, same
    suspended_reason convention); only the code-organisation differs from
    the literal instruction — see this task's final report.
    """
    from backend.app.billing.lifecycle import is_past_grace_period, suspend_for_nonpayment

    async with ctx["session_factory"]() as db:
        try:
            result = await db.execute(
                select(Business).where(Business.billing_status == "PAST_DUE", Business.is_active.is_(True))
            )
            past_due = result.scalars().all()
            suspended = 0
            for business in past_due:
                if is_past_grace_period(business):
                    suspend_for_nonpayment(business)
                    suspended += 1
            if suspended:
                await db.commit()
                logger.warning("Billing grace-period sweep: suspended %d business(es).", suspended)
        except Exception:
            await db.rollback()
            logger.exception("Billing grace-period suspension job failed — rolled back.")


async def sweep_trial_and_invoice_expiry(ctx: dict) -> None:
    """
    Companion sweep to `suspend_past_due_businesses` above, run on the same
    hourly cadence (see WorkerSettings.cron_jobs) but as its own function —
    see that function's docstring NOTE for why this isn't inlined into it.

    1. Suspends any business whose commercial TRIAL expired more than
       TRIAL_EXPIRY_GRACE_DAYS (config) ago and was never converted, using
       `suspended_reason = "TRIAL_EXPIRED"` — a distinct, independently
       reversible reason from non-payment suspension (see
       backend/app/billing/trials.py::_unsuspend_if_trial_expired, which
       only ever lifts a suspension carrying this exact string). Never
       deletes any data.
    2. Marks OVERDUE any issued-unpaid commercial (EFT/cash) invoice past
       its due date (backend/app/billing/commercial_invoices.py).
    """
    from backend.app.billing.commercial_invoices import sweep_overdue_invoices
    from backend.app.core.config import get_settings as _get_settings

    settings = _get_settings()

    async with ctx["session_factory"]() as db:
        try:
            result = await db.execute(
                select(Business).where(
                    Business.billing_status == "TRIAL",
                    Business.is_active.is_(True),
                    Business.trial_ends_at.isnot(None),
                )
            )
            now = datetime.now(timezone.utc)
            trial_suspended = 0
            for business in result.scalars().all():
                deadline = business.trial_ends_at + timedelta(days=settings.TRIAL_EXPIRY_GRACE_DAYS)
                if now > deadline:
                    business.is_active = False
                    business.suspended_reason = "TRIAL_EXPIRED"
                    trial_suspended += 1

            overdue_count = await sweep_overdue_invoices(db)

            if trial_suspended or overdue_count:
                await db.commit()
                logger.warning(
                    "Trial/invoice sweep: suspended %d business(es) for expired trial, "
                    "marked %d invoice(s) OVERDUE.",
                    trial_suspended, overdue_count,
                )
        except Exception:
            await db.rollback()
            logger.exception("Trial/invoice expiry sweep failed — rolled back.")


# ── Cron: Stale WhatsApp connection observability sweep (gate hardening
#    pass, 2026-09-22) ─────────────────────────────────────────────────────

STALE_WHATSAPP_WEBHOOK_DAYS = 3


async def log_stale_whatsapp_connections(ctx: dict) -> None:
    """
    Observability-only sweep: log a WARNING for every CONNECTED
    whatsapp_connections row that hasn't heard from Meta (an inbound
    message OR an account_update event — see
    routes_webhook.py::_stamp_last_webhook) in over
    STALE_WHATSAPP_WEBHOOK_DAYS days. Runs once daily.

    Deliberately does NOT mutate `status`/`status_reason`. A quiet WABA (no
    customer messages that day) is completely normal and is not itself
    evidence of a lost webhook subscription — auto-flipping `status` to
    NEEDS_ATTENTION on staleness alone would risk a false positive with a
    real cost (an owner scared into an unnecessary reconnect), and nothing
    in the existing lifecycle would automatically clear a staleness-caused
    flip the next time a customer messages in (health_check() only clears
    NEEDS_ATTENTION -> CONNECTED on a successful manual/explicit check;
    ordinary inbound-message webhooks never touch `status`, only
    `last_webhook_at`). So this deliberately stays a read-only signal in
    the worker's own logs (matching the WARNING-level "always visible"
    logging convention already used throughout routes_webhook.py) rather
    than inventing an unreviewed auto-suspend-style policy. `last_webhook_at`
    is already exposed per-business via GET /business/whatsapp/connection
    for the owner's own dashboard to build on; a fuller version (dashboard
    surfacing, or a real re-subscribe/notify policy) is a deliberate
    follow-up — see TODO.md.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_WHATSAPP_WEBHOOK_DAYS)
    async with ctx["session_factory"]() as db:
        try:
            result = await db.execute(
                select(WhatsAppConnection).where(
                    WhatsAppConnection.status == "CONNECTED",
                    or_(
                        WhatsAppConnection.last_webhook_at.is_(None),
                        WhatsAppConnection.last_webhook_at < cutoff,
                    ),
                )
            )
            stale = result.scalars().all()
            for connection in stale:
                logger.warning(
                    "WHATSAPP_CONNECTION_STALE: business=%s connection=%s "
                    "last_webhook_at=%s (no Meta activity in over %d days) — "
                    "webhook subscription may have been lost; a manual "
                    "health-check is recommended.",
                    connection.business_id, connection.id,
                    connection.last_webhook_at, STALE_WHATSAPP_WEBHOOK_DAYS,
                )
        except Exception:
            logger.exception("Stale WhatsApp connection sweep failed.")


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

    functions = [
        process_whatsapp_message,
        process_delivery_order,
        process_delivery_cancellation,
    ]
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
        # Cancel UNPAID online-payment orders past their timeout — every 5 minutes.
        cron(
            cancel_unpaid_orders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            second=30,
            run_at_startup=True,
        ),
        # Suspend businesses whose billing grace period has elapsed — hourly.
        cron(
            suspend_past_due_businesses,
            minute=0,
            second=0,
            run_at_startup=True,
        ),
        # Suspend businesses whose trial expired past its grace period, and
        # mark overdue commercial invoices — hourly, same cadence as above
        # (Wave 0A commercial/sales-onboarding push).
        cron(
            sweep_trial_and_invoice_expiry,
            minute=0,
            second=0,
            run_at_startup=True,
        ),
        # Worker liveness heartbeat — every 30 seconds, also on startup so
        # GET /ready sees a fresh heartbeat immediately after a deploy.
        cron(
            write_worker_heartbeat,
            second={0, 30},
            run_at_startup=True,
        ),
        # Log (read-only) any CONNECTED WhatsApp connection that's gone
        # quiet for too long — once daily, off-peak. Gate hardening pass,
        # 2026-09-22 — see log_stale_whatsapp_connections docstring for why
        # this stays observability-only rather than mutating status.
        cron(
            log_stale_whatsapp_connections,
            hour=3,
            minute=0,
            second=0,
            run_at_startup=False,
        ),
        # Delivery-marketplace accept/deny deadline watchdog — every 30s.
        # Auto-DENIES (never auto-accepts, never silently drops) anything
        # still open past DELIVERY_ACCEPT_DEADLINE_SECONDS + grace — see
        # backend/app/delivery/order_service.py::delivery_accept_deadline_watchdog.
        cron(
            delivery_accept_deadline_watchdog,
            second={0, 30},
            run_at_startup=True,
        ),
    ]

    redis_settings = RedisSettings.from_dsn(_settings.REDIS_URL)
