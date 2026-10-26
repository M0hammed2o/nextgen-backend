"""
Webhook plane — the FAST, synchronous half of the order flow (design doc
§5, target <300ms). Verifies, resolves the tenant, dedupe-inserts, and
enqueues; everything slow (fetching the full order, mapping, accept/deny)
happens in the ARQ worker (`order_service.py`).

THE SEVEN GATES, AND WHERE EACH ONE ACTUALLY RUNS
------------------------------------------------------
The design doc (§4) describes seven ordered gates: signature -> store
resolution -> environment match -> tenant active -> entitlement ->
connection status -> (idempotent) enqueue. This module owns the first three
(signature, store resolution, and — as a "known store" cutoff — nothing
further blocks here); `order_service.py` owns environment match, tenant
active, entitlement, and connection status, because ONLY THOSE FOUR gates
correspond to a case where we already know which business this is and
therefore have a "misconfiguration -> active deny_pos_order" obligation
(design §7) that requires a live provider network call — something this
module deliberately never does, to keep the request-path handler fast.

Concretely:
  - UNKNOWN store (envelope's store id resolves to no connection at all):
    stop HERE. Logged with error_category=UNKNOWN_STORE, no DeliveryOrder
    row is created, no job is enqueued — "we have no right to deny an
    order for a restaurant that isn't our customer" (design §7).
  - KNOWN store (connection resolves, regardless of its environment/status,
    and regardless of the tenant's entitlement): proceeds all the way to an
    idempotent DeliveryOrder row and a worker enqueue. The worker checks
    environment/tenant-active/entitlement/connection-status FIRST, before
    ever calling Get Order Details, and actively denies (not silence) on
    any of those four failing — see `order_service.py` module docstring.
This split (rather than every gate in this module) is this change's own
resolution of an ambiguity between design §4's gate list and §5's "fast
synchronous handler" framing — flagged explicitly in this change's report
as an implementation decision, not a literal transcription of the design
doc's file split.

IDEMPOTENCY — INSERT ... ON CONFLICT DO NOTHING (design §3)
-----------------------------------------------------------------
Two independent conflict targets are used, exactly per the design doc:
  1. `delivery_webhook_events (provider, event_id)` — a concurrently
     re-delivered identical webhook produces exactly one event-log row; the
     second (or Nth) delivery detects "0 rows inserted" and returns 200
     without enqueuing a second job.
  2. `delivery_orders (business_id, provider, external_order_id)` — always
     attempted (whether this is a brand-new order-notification or a later
     event like a cancellation for an order we already know about) with
     status='RECEIVED' as the only value it would take if newly inserted.
     On conflict, this is a pure no-op — the EXISTING row (whatever state
     it's actually in) is left completely untouched, and the worker job
     that gets enqueued re-reads it fresh. This is why it's safe to run
     unconditionally regardless of the webhook's actual event type.
Both use SQLAlchemy's `postgresql.insert(...).on_conflict_do_nothing(
index_elements=[...])`, which — unlike a bare try/except IntegrityError —
suppresses ONLY the named conflict target: an unrelated constraint
violation (a real bug) still raises normally and is never masked. This is
the same safety property `routes_pos.py::_replay_idempotent_checkout`
achieves by inspecting the constraint name after the fact; here it's
enforced by Postgres itself, which is possible because the conflict target
is known upfront (unlike a checkout idempotency key, which really can hit
one of several distinct constraints depending on the race).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.delivery import registry
from backend.app.delivery.canonical import WebhookGateResult
from backend.app.delivery.tenant_resolver import resolve_delivery_connection
from shared.models.delivery import DeliveryOrder, DeliveryWebhookEvent

logger = logging.getLogger("nextgen.delivery.intake")

_RAW_PAYLOAD_MAX_BYTES = 64 * 1024  # bound what we store, never the full 10MB order response

#: Event types routed to the cancellation job rather than the order job —
#: kept in sync with order_service.py's own copy (which also redirects
#: internally as defense-in-depth if this module's routing is ever bypassed).
_CANCEL_EVENT_TYPES = frozenset({"orders.cancel", "orders.failure"})

#: Uber: a scheduled order must be accepted "within 10 minutes of
#: orders.scheduled.notification" — tighter than the normal 11.5-minute
#: window, so its deadline is capped below 10 minutes INCLUDING the
#: watchdog's grace period.
_SCHEDULED_EVENT_TYPE = "orders.scheduled.notification"
_SCHEDULED_ACCEPT_WINDOW_SECONDS = 600


def _bounded_payload(raw_body: bytes) -> dict:
    import json

    try:
        payload = json.loads(raw_body[:_RAW_PAYLOAD_MAX_BYTES] or b"{}")
        return payload if isinstance(payload, dict) else {"_unparsed": True}
    except (ValueError, TypeError, RecursionError):
        # RecursionError (BUG-0090): a deeply-nested body (e.g. tens of
        # thousands of unclosed `[`) can blow CPython's json decoder's
        # recursion depth well under the 64KB truncation bound above, and
        # this is reachable with NO valid signature (bad-signature events
        # still get logged, via this same helper). Must degrade to the same
        # sentinel as any other unparseable body, never escape and 500 the
        # always-200 webhook contract.
        return {"_unparsed": True}


async def ingest_delivery_webhook(
    db: AsyncSession,
    *,
    provider: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    arq_pool,
) -> WebhookGateResult:
    """
    Run the fast synchronous plane. ALWAYS returns http_status=200 unless
    the final enqueue step itself fails (503) — see module docstring and
    design doc §4's "Uber requires an ack" rule. Callers (the route) must
    still call `await request.body()` themselves BEFORE any parsing and
    pass those exact raw bytes — the signature is over the raw body.
    """
    settings = get_settings()

    if not registry.is_webhook_enabled(provider) or not settings.UBER_EATS_WEBHOOK_ENABLED:
        # Covers both "not a provider we ever act on webhooks for" (MR_D —
        # see registry.py) AND the platform-wide kill switch. registry.py's
        # own _WEBHOOK_ENABLED_PROVIDERS is the single source of truth for
        # provider membership (previously this was a hardcoded, case-
        # sensitive `provider != "UBER_EATS"` string check that could
        # silently drift from registry.py — BUG-0094). No DB write at all —
        # logging an attempted webhook for an unsupported provider string
        # adds nothing an access log doesn't already have, and avoids ever
        # writing an unvalidated provider string into a column.
        logger.warning(
            "DELIVERY_WEBHOOK_REJECTED: provider=%r not webhook-enabled "
            "(unsupported provider or platform kill switch).", provider,
        )
        return WebhookGateResult(accepted=False, error_category="UNSUPPORTED_PROVIDER")

    adapter = registry.get_provider(provider)
    if adapter is None:  # pragma: no cover — is_webhook_enabled already guarantees this
        return WebhookGateResult(accepted=False, error_category="UNSUPPORTED_PROVIDER")

    # The environment is PROVEN by which app secret verifies the signature
    # (production and sandbox are separate Uber apps) — never taken from the
    # unauthenticated `X-Environment` header, which only gets logged.
    verified_environment = None
    try:
        verified_environment = adapter.verified_webhook_environment(raw_body, headers)
    except Exception:
        logger.exception("DELIVERY_WEBHOOK_SIGNATURE_CHECK_ERROR: provider=%s", provider)
    sig_valid = verified_environment is not None

    envelope = None
    try:
        envelope = adapter.parse_webhook_event(raw_body, headers)
    except Exception:
        logger.exception("DELIVERY_WEBHOOK_PARSE_ERROR: provider=%s", provider)

    reported_environment = headers.get("x-environment") or headers.get("X-Environment")
    if sig_valid and reported_environment and reported_environment.lower() != verified_environment:
        logger.warning(
            "DELIVERY_WEBHOOK_ENV_HEADER_DISAGREES: provider=%s header=%r verified=%s — "
            "trusting the signature, not the header.",
            provider, reported_environment, verified_environment,
        )

    if not sig_valid:
        # BUG-0088 fix: an unverified request must NEVER be able to occupy
        # the (provider, event_id) namespace that gates whether a later
        # VERIFIED delivery of the same event_id gets processed. Previously
        # this branch inserted under the attacker-supplied `envelope.
        # event_id` whenever the body happened to parse — an attacker who
        # could predict/guess a real Uber event_id could pre-claim it with
        # zero authentication, and the genuine signed delivery would then
        # hit ON CONFLICT DO NOTHING and be silently swallowed as
        # "duplicate_event" with no delivery_orders row for the watchdog to
        # rescue. Always synthesize a fresh id here instead — the attacker-
        # supplied event_id (if any) is still fully preserved for audit
        # inside raw_payload_json (the JSON body's own `event_id` field), it
        # just never becomes the DEDUPE KEY. This also means a genuine sender
        # whose signature only failed due to OUR OWN misconfiguration (e.g.
        # BUG-0089 — a missing UBER_EATS_CLIENT_SECRET) self-heals cleanly
        # once fixed: the real event_id was never consumed by the bad-
        # signature row, so the next (retried) delivery inserts cleanly.
        await _insert_event_idempotent(
            db,
            provider=provider,
            event_id=f"unverified:{uuid.uuid4()}",
            external_store_id=envelope.external_store_id if envelope else None,
            event_type=envelope.event_type if envelope else None,
            raw_body=raw_body, signature_valid=False,
            error_category="BAD_SIGNATURE", environment=reported_environment,
        )
        await db.commit()
        logger.warning("DELIVERY_WEBHOOK_BAD_SIGNATURE: provider=%s", provider)
        return WebhookGateResult(accepted=False, error_category="BAD_SIGNATURE")

    if envelope is None or not envelope.event_id:
        await _insert_event_idempotent(
            db, provider=provider, event_id=str(uuid.uuid4()), external_store_id=None,
            event_type=None, raw_body=raw_body, signature_valid=True,
            error_category="UNPARSEABLE_PAYLOAD", environment=verified_environment,
        )
        await db.commit()
        logger.warning("DELIVERY_WEBHOOK_UNPARSEABLE: provider=%s", provider)
        return WebhookGateResult(accepted=False, error_category="UNPARSEABLE_PAYLOAD")

    # ── Event-log idempotency: ON CONFLICT DO NOTHING on (provider, event_id) ──
    # Production keeps Uber's raw event_id (unchanged from before environments
    # existed); any other environment's ids live in their own namespace, so an
    # event signed with the sandbox secret can never occupy a production
    # event_id and get the genuine production delivery deduplicated away.
    dedupe_event_id = (
        envelope.event_id if verified_environment == "production"
        else f"{verified_environment}:{envelope.event_id}"
    )[:128]
    event_row, inserted = await _insert_event_idempotent(
        db, provider=provider, event_id=dedupe_event_id,
        external_store_id=envelope.external_store_id, event_type=envelope.event_type,
        raw_body=raw_body, signature_valid=True, error_category="PENDING",
        environment=verified_environment,
    )
    if not inserted:
        await db.commit()
        logger.info(
            "DELIVERY_WEBHOOK_DUPLICATE_EVENT: provider=%s event_id=%s — already processed.",
            provider, envelope.event_id,
        )
        return WebhookGateResult(accepted=True, error_category=None, detail="duplicate_event")

    connection = await resolve_delivery_connection(
        db, provider=provider, external_store_id=envelope.external_store_id
    )
    if connection is None:
        event_row.error_category = "UNKNOWN_STORE"
        await db.commit()
        logger.warning(
            "DELIVERY_WEBHOOK_UNKNOWN_STORE: provider=%s external_store_id=%s — "
            "not our customer, ignored (no deny issued).",
            provider, envelope.external_store_id,
        )
        return WebhookGateResult(accepted=True, error_category="UNKNOWN_STORE")

    # The environment PROVEN by the signature must be the connection's own.
    # Checked here, before any delivery_orders row or job exists, for EVERY
    # event type: a cancellation used to skip the worker's ENV_MISMATCH gate,
    # so an event signed with the (weaker-held) sandbox secret could cancel a
    # production order and reverse its stock. A mismatched event is logged and
    # acknowledged, nothing else — it belongs to the other environment's app,
    # which no NextGen connection here represents.
    if verified_environment != connection.environment:
        event_row.business_id = connection.business_id
        event_row.error_category = "ENV_MISMATCH"
        await db.commit()
        logger.warning(
            "DELIVERY_WEBHOOK_ENV_MISMATCH: provider=%s business=%s signed_for=%s "
            "connection_environment=%s event_type=%s — ignored.",
            provider, connection.business_id, verified_environment,
            connection.environment, envelope.event_type,
        )
        return WebhookGateResult(
            accepted=False, error_category="ENV_MISMATCH",
            business_id=connection.business_id, delivery_connection_id=connection.id,
        )

    # Known store from here on — business_id comes ONLY from `connection`,
    # never from the payload. All remaining checks (environment match,
    # tenant active, entitlement, connection status) are deferred to the
    # worker, which can issue an active deny — see module docstring.
    event_row.business_id = connection.business_id
    event_row.error_category = "OK"

    # BUG-0094 item 3: a non-order event (e.g. `store.status.changed`) has
    # no `resource_id` — there is nothing to fetch, map, or enqueue a worker
    # job for. Previously this unconditionally enqueued `process_delivery_
    # order` with `external_order_id=None`, and the worker then logged a
    # false DELIVERY_ORDER_ROW_MISSING ERROR on every such event (its own
    # message claims "intake.py should have created this row", which is
    # simply not true for this event class). Commit the event-log row (audit
    # trail preserved) and ack — nothing else to do.
    if not envelope.external_order_id:
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("DELIVERY_WEBHOOK_DB_COMMIT_FAILED: provider=%s", provider)
            return WebhookGateResult(accepted=False, http_status=503, error_category="DB_ERROR")
        return WebhookGateResult(
            accepted=True, error_category="OK",
            business_id=connection.business_id, delivery_connection_id=connection.id,
        )

    await _insert_delivery_order_idempotent(
        db,
        business_id=connection.business_id,
        provider=provider,
        connection_id=connection.id,
        external_order_id=envelope.external_order_id,
        external_store_id=envelope.external_store_id,
        raw_event_id=event_row.id,
        event_type=envelope.event_type,
    )

    # BUG-0087 fix: the event-log/delivery-order rows are deliberately NOT
    # committed until the enqueue itself has succeeded. Previously this
    # committed FIRST, then attempted the enqueue — so an enqueue failure
    # (a transient Redis blip) returned 503 "so the provider retries
    # delivery", but the retry (same event_id) always hit the ALREADY-
    # COMMITTED row and short-circuited as `duplicate_event` at the ON
    # CONFLICT check above, silently dropping a real paid order forever. By
    # flushing (not committing) here, holding this transaction open across
    # the enqueue call, and rolling back on enqueue failure, a genuinely
    # failed attempt leaves NOTHING behind: the (provider, event_id) row was
    # never actually persisted, so an identical retry inserts cleanly and
    # gets a fresh chance to enqueue. A concurrent duplicate delivery
    # arriving while this transaction is still open blocks briefly on the
    # same unique-index row lock (standard Postgres behaviour) rather than
    # racing past it — it does not weaken the dedupe guarantee.
    job_name = "process_delivery_cancellation" if envelope.event_type in _CANCEL_EVENT_TYPES else "process_delivery_order"
    job_args = (
        (provider, str(connection.business_id), envelope.external_order_id)
        if job_name == "process_delivery_cancellation"
        else (provider, str(connection.business_id), envelope.external_order_id, envelope.event_type)
    )
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        logger.exception("DELIVERY_WEBHOOK_DB_FLUSH_FAILED: provider=%s", provider)
        return WebhookGateResult(accepted=False, http_status=503, error_category="DB_ERROR")

    try:
        await arq_pool.enqueue_job(job_name, *job_args)
    except Exception:
        await db.rollback()
        logger.exception(
            "DELIVERY_WEBHOOK_ENQUEUE_FAILED: provider=%s business_id=%s external_order_id=%s — "
            "rolled back (nothing committed) so a retried delivery of the SAME event_id can "
            "insert cleanly and try again; returning 503 so the provider retries delivery.",
            provider, connection.business_id, envelope.external_order_id,
        )
        return WebhookGateResult(
            accepted=False, http_status=503, error_category="ENQUEUE_FAILED",
            business_id=connection.business_id,
        )

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("DELIVERY_WEBHOOK_DB_COMMIT_FAILED: provider=%s", provider)
        return WebhookGateResult(accepted=False, http_status=503, error_category="DB_ERROR")

    return WebhookGateResult(
        accepted=True, error_category="OK",
        business_id=connection.business_id, delivery_connection_id=connection.id,
    )


async def _insert_event_idempotent(
    db: AsyncSession,
    *,
    provider: str,
    event_id: str,
    external_store_id: str | None,
    event_type: str | None,
    raw_body: bytes,
    signature_valid: bool,
    error_category: str,
    environment: str | None,
) -> tuple[DeliveryWebhookEvent, bool]:
    """
    INSERT ... ON CONFLICT (provider, event_id) DO NOTHING. Returns the
    persisted row (existing OR newly inserted) and whether THIS call
    actually inserted it. Never raises on a duplicate — only an UNRELATED
    constraint violation (a real bug) would still propagate, since
    ON CONFLICT only suppresses the named target.
    """
    stmt = (
        pg_insert(DeliveryWebhookEvent)
        .values(
            provider=provider,
            event_id=event_id,
            external_store_id=external_store_id,
            business_id=None,
            event_type=event_type,
            environment=environment,
            signature_valid=signature_valid,
            error_category=error_category,
            raw_payload_json=_bounded_payload(raw_body),
            received_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(
            index_elements=["provider", "event_id"],
        )
        .returning(DeliveryWebhookEvent.id)
    )
    result = await db.execute(stmt)
    new_id = result.scalar_one_or_none()
    if new_id is not None:
        row = await db.get(DeliveryWebhookEvent, new_id)
        return row, True

    existing = (
        await db.execute(
            select(DeliveryWebhookEvent).where(
                DeliveryWebhookEvent.provider == provider,
                DeliveryWebhookEvent.event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    return existing, False


async def _insert_delivery_order_idempotent(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    provider: str,
    connection_id: uuid.UUID,
    external_order_id: str,
    external_store_id: str | None,
    raw_event_id: uuid.UUID,
    event_type: str | None = None,
) -> None:
    """
    INSERT ... ON CONFLICT (business_id, provider, external_order_id) DO
    NOTHING — a pure no-op when the order is already known, whatever state
    it's actually in. See module docstring for why this is safe to call
    unconditionally for every event type.
    """
    settings = get_settings()
    deadline_seconds = settings.DELIVERY_ACCEPT_DEADLINE_SECONDS
    if event_type == _SCHEDULED_EVENT_TYPE:
        deadline_seconds = min(
            deadline_seconds,
            _SCHEDULED_ACCEPT_WINDOW_SECONDS - settings.DELIVERY_DEADLINE_GRACE_SECONDS - 60,
        )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)
    stmt = (
        pg_insert(DeliveryOrder)
        .values(
            business_id=business_id,
            provider=provider,
            delivery_connection_id=connection_id,
            external_order_id=external_order_id,
            external_store_id=external_store_id or "",
            raw_event_id=raw_event_id,
            status="RECEIVED",
            accept_deadline_at=deadline,
        )
        .on_conflict_do_nothing(
            index_elements=["business_id", "provider", "external_order_id"],
        )
    )
    await db.execute(stmt)
