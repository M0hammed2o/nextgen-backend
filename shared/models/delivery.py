"""
Delivery marketplace integration: live API connections (Uber Eats today),
provider order lifecycle, and the raw webhook event log.

WHY THIS IS NOT A SECOND "external_orders.py"
-----------------------------------------------
`external_orders.py`'s own docstring explains why an imported sale becomes a
real ``Order`` rather than a parallel entity — one canonical product, one
canonical recipe, one stock ledger. That rule still holds here: a delivery
order that gets accepted still becomes a real ``Order`` via the same
``post_sale_consumption`` path the CSV importer uses.

What's different about a LIVE API connection is that an order has a
*lifecycle before it becomes a sale* — an unmapped item, an accept/deny
handshake with an 11.5-minute provider deadline, a cancellation — that a
CSV row (already a finished, past-tense fact) never had anywhere to live.
``DeliveryOrder`` is a strictly-scoped lifecycle/reconciliation SIDECAR for
exactly that in-flight period, not a second sales system:

* It has no line-item child table. Line items exist exactly twice: once as
  a JSONB intake snapshot on THIS row (``fetched_order_snapshot_json``),
  and once as real ``OrderItem`` rows once mapping succeeds and the
  canonical ``Order`` is created.
* ``backend/app/analytics/revenue.py`` must never read this table — the
  real ``Order`` it eventually produces is the only thing that counts as a
  sale. Money columns here (`gross_cents`/`fee_cents`/`restaurant_net_cents`)
  are reconciliation-only.
* Inventory reads only the created ``Order`` (same `post_sale_consumption`
  call the CSV path already makes) — never this table.
* Created orders get `Order.till_session_id = None`, same as every existing
  external-platform order — they must never reach till cash-up.

See DECISIONS.md DECISION-0030 for the qualified-exception record, and
`docs/DELIVERY_PLATFORM_ARCHITECTURE.md` for the full design.

TENANT RESOLUTION IS A DATABASE FACT, NOT APPLICATION LOGIC
-------------------------------------------------------------
``delivery_connections`` carries the single most important constraint in
this module: a partial UNIQUE index on ``(provider, external_store_id)
WHERE external_store_id IS NOT NULL``. A webhook has no JWT — the ONLY
permitted source of ``business_id`` for an inbound delivery webhook is this
table, looked up by ``(provider, external_store_id)``. No webhook field may
ever contribute to, override, or hint at a ``business_id`` — see
`backend/app/delivery/tenant_resolver.py`.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DeliveryConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    One row per (business, provider) — this tenant's live connection to a
    delivery marketplace.

    The provisioning token (per-merchant OAuth `authorization_code` grant,
    scope `eats.pos_provisioning` ONLY) is Fernet-encrypted at rest via
    `backend/app/core/crypto.py` and wiped (set to NULL) immediately after
    store activation succeeds — it is never returned in any API response and
    is not needed again afterwards (the operational `client_credentials`
    token used for every subsequent call is platform-wide, cached in Redis,
    and never stored in Postgres at all — see `backend/app/delivery/uber/auth.py`).
    """

    __tablename__ = "delivery_connections"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "provider",
            name="uq_delivery_connections_business_provider",
        ),
        # THE trust-boundary constraint. Partial because a NOT_CONNECTED
        # connection (provisioning started, store not yet activated) has no
        # store id yet — modelled directly on migration 0037's WABA-uniqueness fix.
        Index(
            "uq_delivery_connections_provider_external_store_id",
            "provider", "external_store_id",
            unique=True,
            postgresql_where=text("external_store_id IS NOT NULL"),
        ),
        Index("ix_delivery_connections_business_id", "business_id"),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="UBER_EATS / MR_D"
    )
    external_store_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="Provider's store id — THE tenant key for inbound webhooks. "
                "NULL until activation completes.",
    )
    status: Mapped[str] = mapped_column(
        String(24), default="NOT_CONNECTED", nullable=False,
        comment="NOT_CONNECTED / SANDBOX / AWAITING_APPROVAL / CONNECTED / "
                "ATTENTION_REQUIRED / DISABLED. Commercial-correctness "
                "vocabulary (not a generic PENDING/ACTIVE/PAUSED set): Uber "
                "Eats production access requires Uber's own written "
                "approval, so a restaurant can be wired up (SANDBOX) or "
                "waiting on Uber (AWAITING_APPROVAL) while unable to take a "
                "single real order — the UI must never say 'Live "
                "Integration - Connected' unless status == CONNECTED. "
                "ATTENTION_REQUIRED is the error/degraded state (repeated "
                "auto-denies, Uber-side pause, etc). DISABLED is an "
                "operator switch-off, distinct from a provider-side "
                "disconnect (which surfaces as ATTENTION_REQUIRED). Only "
                "CONNECTED counts as live — see entitlements.py::is_connection_live.",
    )
    environment: Mapped[str] = mapped_column(
        String(16), default="production", nullable=False,
        comment="production / sandbox — must match the webhook's own "
                "X-Environment header before any event from it is trusted.",
    )
    auto_accept_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Defaults FALSE. When false, a fully-mapped delivery order "
                "stops at READY_TO_ACCEPT and waits for an explicit staff "
                "accept (POST .../orders/{id}/accept) instead of "
                "auto-committing to the kitchen — see migration 0046. When "
                "true, order_service._handle_order auto-accepts any fully-"
                "resolved order exactly as it always has.",
    )

    provisioning_token_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Fernet-encrypted per-merchant authorization_code token "
                "(eats.pos_provisioning scope only). Wiped after activation.",
    )
    store_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="Cached ONLINE / PAUSED from the provider."
    )
    last_status_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    def __repr__(self) -> str:
        return f"<DeliveryConnection {self.provider} store={self.external_store_id!r} [{self.status}]>"


class DeliveryWebhookEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Raw inbound webhook event log — the first table on this platform with a
    defined retention policy (redact the payload at 30 days via
    `payload_redacted_at`, delete the row at 400 — see
    `backend/app/delivery/intake.py`). The Get-Order-Details response itself
    is never persisted here or anywhere else raw; only allow-listed, mapped
    fields ever land in a column (see `DeliveryOrder.fetched_order_snapshot_json`).

    Idempotency at the EVENT-LOG level: `UniqueConstraint(provider, event_id)`
    is the first of three stacked uniqueness guarantees for a duplicate
    webhook delivery — see module docstring on `DeliveryOrder` for the other
    two (delivery-order intake, and the pre-existing
    `uq_orders_business_source_external_id`).

    `business_id` is nullable and resolved SEPARATELY from parsing — an
    unknown store id is logged here with `business_id=NULL` and
    `error_category='UNKNOWN_STORE'` rather than refused outright, since we
    have no right to deny an order for a restaurant that isn't our customer.
    """

    __tablename__ = "delivery_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "provider", "event_id",
            name="uq_delivery_webhook_events_provider_event_id",
        ),
        Index("ix_delivery_webhook_events_business_id", "business_id"),
        Index("ix_delivery_webhook_events_received_at", "received_at"),
    )

    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    event_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Provider's own event_id"
    )
    external_store_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="As reported by the webhook, BEFORE trust — never used to "
                "select data on its own, only to look up delivery_connections.",
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True,
        comment="NULL until/unless (provider, external_store_id) resolves "
                "to a known connection. Never sourced from the payload directly.",
    )
    event_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="orders.notification / orders.cancel / store.status.changed / ...",
    )
    environment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="BAD_SIGNATURE / UNKNOWN_STORE / ENV_MISMATCH / TENANT_INACTIVE / "
                "ENTITLEMENT_OFF / CONNECTION_NOT_LIVE / ENQUEUE_FAILED / OK",
    )
    raw_payload_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="The webhook envelope only (never a followed resource_href, "
                "never a persisted Get-Order-Details response). Redacted at "
                "30 days (payload_redacted_at set, content cleared).",
    )
    payload_redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=text("now()"),
    )

    def __repr__(self) -> str:
        return f"<DeliveryWebhookEvent {self.provider} event_id={self.event_id!r}>"


class DeliveryOrder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Provider-side lifecycle & reconciliation sidecar for one delivery-API
    order — see module docstring for why this exists and what it is NOT.

    Idempotency at the DELIVERY-ORDER level: `UniqueConstraint(business_id,
    provider, external_order_id)` — business-SCOPED, not global, because the
    same Uber order id must remain independently importable by a different
    restaurant (existing DECISIONS.md rule, carried over unchanged from the
    CSV importer's `uq_orders_business_source_external_id`).
    """

    __tablename__ = "delivery_orders"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "provider", "external_order_id",
            name="uq_delivery_orders_business_provider_external_order_id",
        ),
        Index("ix_delivery_orders_business_id", "business_id"),
        Index("ix_delivery_orders_status", "status"),
        # Partial: the watchdog only ever scans open/live rows, so this stays
        # small permanently instead of growing with the full order history.
        Index(
            "ix_delivery_orders_accept_deadline_at",
            "accept_deadline_at",
            postgresql_where=text(
                "status IN ('RECEIVED', 'FETCHING', 'NEEDS_MAPPING', 'READY_TO_ACCEPT')"
            ),
        ),
        # One delivery order maps to at most one canonical Order. Partial
        # because most rows never reach mapping (NEEDS_MAPPING/DENIED/FAILED
        # never get an order_id at all, by design).
        Index(
            "uq_delivery_orders_order_id",
            "order_id",
            unique=True,
            postgresql_where=text("order_id IS NOT NULL"),
        ),
        # Non-unique: provider display codes are short and reused over time.
        # Read by the CSV importer to refuse a second sale for an order the
        # live API already booked (migration 0059).
        Index(
            "ix_delivery_orders_business_provider_display_id",
            "business_id", "provider", "display_id",
            postgresql_where=text("display_id IS NOT NULL"),
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("delivery_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Provider's short human order code (Uber display_id) — what a "
                "courier quotes at the counter. Not unique. Migration 0059.",
    )
    external_store_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Snapshot for audit, not used for lookups."
    )
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("delivery_webhook_events.id", ondelete="SET NULL"),
        nullable=True,
        comment="The webhook event that first created this row.",
    )

    status: Mapped[str] = mapped_column(
        String(24), default="RECEIVED", nullable=False,
        comment="RECEIVED / FETCHING / NEEDS_MAPPING / READY_TO_ACCEPT / "
                "ACCEPTED / ACCEPT_UNCONFIRMED / DENIED / FULFILLED / CANCELLED / "
                "FAILED / UNKNOWN (UNKNOWN added to the CHECK constraint in "
                "migration 0045 — BUG-0094 item 4; ACCEPT_UNCONFIRMED in 0059 — "
                "the sale stands but the provider never confirmed our accept, "
                "so the deadline watchdog must not deny it)",
    )
    # The real canonical sale, once (and only if/when) it exists. Nullable —
    # a NEEDS_MAPPING / DENIED / FAILED order never gets one, by design: an
    # unmapped item never auto-creates a sale.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
    )

    accept_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="received_at + DELIVERY_ACCEPT_DEADLINE_SECONDS (600s, "
                "deliberately under Uber's real 690s deadline). The "
                "watchdog cron auto-denies anything still open past this.",
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    denied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deny_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deny_reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reconciliation-only. backend/app/analytics/revenue.py must NEVER read
    # these — the real Order (subtotal/tax/total_cents) is the sale of record.
    gross_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restaurant_net_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="NULL at intake by design — Get Order Details has no payout/"
                "commission data (needs the separate eats.report scope, "
                "Phase 2). Never inferred as total - fee.",
    )
    currency: Mapped[str] = mapped_column(String(3), default="ZAR", nullable=False)

    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fetched_order_snapshot_json: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True,
        comment="Allow-listed, mapped fields ONLY — never the raw provider "
                "response. See backend/app/delivery/uber/mapping.py.",
    )
    unresolved_items_json: Mapped[list | None] = mapped_column(
        JSONB, nullable=True,
        comment="Item/modifier text that failed to resolve against "
                "external_product_mappings, surfaced for manual staff mapping.",
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Outcome of the most recent outbound status push to the provider
    # (ready-for-pickup, restaurant-initiated cancel) — migration 0059.
    last_provider_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_provider_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DeliveryOrder {self.provider} external_order_id={self.external_order_id!r} [{self.status}]>"
