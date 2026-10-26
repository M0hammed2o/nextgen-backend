"""
Canonical delivery-order shapes.

PURE DATACLASSES — NO SQLALCHEMY, NO DATABASE, NO NETWORK
-----------------------------------------------------------
Same discipline as `backend/app/external_orders/adapters.py`'s
`CanonicalExternalOrder`: nothing downstream of a provider adapter's
`parse_webhook_event`/`map_order` functions knows what Uber Eats calls its
own fields. This is the seam every unit test drives, and the reason a
second provider (or a real Mr D integration, if one is ever built) never
has to touch `intake.py`/`order_service.py`.

WHY `business_id` IS NOT A FIELD HERE
----------------------------------------
It's resolved from `external_store_id` only at persistence time
(`backend/app/delivery/tenant_resolver.py`) — never anywhere in this
module — so a provider payload can never carry, imply, or influence a
tenant. See `docs/DELIVERY_PLATFORM_ARCHITECTURE.md` §2/§4.

WHY `delivery_address` IS ALWAYS None
----------------------------------------
Uber's Get Order Details response does not expose the eater's full address
to a POS integration (confirmed from the official docs — `eater` carries
only `first_name`/`phone`/`phone_code`). Typing this field `None` by
construction, rather than omitting it, means nothing downstream can start
silently depending on data that doesn't exist.

WHY `restaurant_net_cents` IS ALWAYS None AT INTAKE
------------------------------------------------------
Get Order Details has no payout/commission data — that needs the separate
`eats.report` scope (Phase 2, not built here). It must never be inferred as
`total - fee`, so nothing in this module ever computes it.

WHY `provider_metadata` IS AN ALLOW-LIST
--------------------------------------------
`build_provider_metadata()` copies named fields out of a raw payload; it
never does `{k: v for k, v in raw.items() if k not in DENY_LIST}`. That is
what keeps eater PII out of a JSONB column by construction, not vigilance.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime


class DeliveryOrderStatus(str, enum.Enum):
    """
    Our relationship with the PROVIDER for one delivery order — deliberately
    separate from the kitchen's own `OrderStatus` (shared/enums.py), which
    tracks food-preparation state on the real `Order` once one exists.

    RECEIVED        - webhook accepted and enqueued, nothing fetched yet
    FETCHING        - worker is calling Get Order Details
    NEEDS_MAPPING   - fetched, but one or more items/modifiers have no
                      resolved menu mapping. Never auto-created a sale,
                      never guessed. Surfaced for a human, ticking against
                      the real provider deadline.
    READY_TO_ACCEPT - fully mapped, accept/deny handshake outstanding
    ACCEPTED        - accept_pos_order succeeded; the real Order now exists
    ACCEPT_UNCONFIRMED - the real Order exists and the kitchen has it, but
                      the accept call timed out / failed without proving the
                      provider rejected it. Never auto-denied by the watchdog
                      (the food is being made); a human confirms on the
                      provider's side or retries the accept.
    DENIED          - deny_pos_order succeeded (or was forced by the
                      accept-deadline watchdog)
    FULFILLED       - terminal, order completed on the provider side
    CANCELLED       - provider or eater cancelled after acceptance
    FAILED          - an unrecoverable error (fetch failed after retries,
                      etc.) — never silently dropped, always visible here
    UNKNOWN         - the provider reported a state this adapter doesn't
                      recognise. Raises an alert rather than guessing.
    """
    RECEIVED = "RECEIVED"
    FETCHING = "FETCHING"
    NEEDS_MAPPING = "NEEDS_MAPPING"
    READY_TO_ACCEPT = "READY_TO_ACCEPT"
    ACCEPTED = "ACCEPTED"
    ACCEPT_UNCONFIRMED = "ACCEPT_UNCONFIRMED"
    DENIED = "DENIED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DeliveryConnectionStatus(str, enum.Enum):
    """
    Commercial-correctness vocabulary — deliberately NOT a generic
    PENDING/ACTIVE/PAUSED/DISCONNECTED/ERROR set. Uber Eats production
    access requires Uber's own written approval, so a restaurant can be
    wired up to a SANDBOX environment or waiting on Uber's own review
    (AWAITING_APPROVAL) while unable to take a single real order. The
    product requirement is that the UI must never show "Live Integration -
    Connected" unless the integration genuinely is live and approved — only
    CONNECTED means that (see `entitlements.py::is_connection_live`).

    NOT_CONNECTED      - no live connection (initial state; also default)
    SANDBOX            - wired up against Uber's sandbox only, no real orders
    AWAITING_APPROVAL  - production credentials in place, awaiting Uber's
                         own written go-live approval
    CONNECTED          - genuinely live and approved; the ONLY status that
                         may process a real order or read "Connected" in
                         any UI
    ATTENTION_REQUIRED - degraded/error (repeated auto-denies, Uber itself
                         pausing the store, a fetch/deny failure, etc.)
    DISABLED           - an explicit NextGen-operator switch-off, distinct
                         from a provider-side disconnect/pause (which
                         surfaces as ATTENTION_REQUIRED, not this)
    """
    NOT_CONNECTED = "NOT_CONNECTED"
    SANDBOX = "SANDBOX"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CONNECTED = "CONNECTED"
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class CanonicalDeliveryModifier:
    """
    One modifier on a line, still in the provider's own words.

    `removed=True` is a DEFAULT ingredient the customer took out (Uber's
    `removed_items`, e.g. "no sugar") — it must reduce stock, never add a
    charge. `group_title` is the provider's modifier-group name ("Choose
    size"), kept so a manager can tell what a bare option title belongs to.
    """
    external_id: str | None
    title: str
    quantity: int = 1
    price_cents: int | None = None
    removed: bool = False
    group_external_id: str | None = None
    group_title: str | None = None


@dataclass(frozen=True)
class CanonicalDeliveryLine:
    """
    One sold item on a delivery order.

    `external_item_id` is the provider's stable id for this menu entity
    (Uber's cart item `external_data`, when the restaurant's own menu was
    used as the source of truth for the push, or the provider's own item id
    otherwise) — THE mapping key against `external_product_mappings.
    external_id`. `title` is kept purely for a human-readable fallback and
    for exact-name auto-matching, same conservatism as the CSV path.
    """
    external_item_id: str | None
    title: str
    quantity: int
    # Provider's per-unit price INCLUDING selected modifiers (Uber
    # `price.unit_price`), the line total (`price.total_price`) and the
    # per-unit price before modifiers (`price.base_unit_price`) — all the
    # channel's own prices, which can legitimately differ from the in-store
    # menu price.
    unit_price_cents: int | None = None
    modifiers: tuple[CanonicalDeliveryModifier, ...] = ()
    special_instructions: str | None = None
    total_price_cents: int | None = None
    base_unit_price_cents: int | None = None


@dataclass
class CanonicalDeliveryOrder:
    """
    One delivery-marketplace order, normalised. Still platform vocabulary —
    resolving against this tenant's menu happens later, in `intake.py`
    against saved `external_product_mappings` rows.
    """
    provider: str
    external_order_id: str
    external_store_id: str
    lines: list[CanonicalDeliveryLine] = field(default_factory=list)

    display_id: str | None = None
    provider_status_raw: str | None = None
    order_type: str | None = None
    placed_at: datetime | None = None
    ready_for_pickup_at: datetime | None = None
    currency: str = "ZAR"

    # Uber's own definitions (v2 Get Order Details, read 2026-10-26):
    #   sub_total — "The total cost of each line item in the cart."
    #   total     — "The cost of the entire order, including taxes and fees
    #               that will be paid out to the Merchant excluding
    #               Marketplace Fees."
    #   total_fee — "The total of the fees charged on the order that will be
    #               paid out to the Merchant."
    subtotal_cents: int | None = None
    tax_cents: int | None = None
    fee_cents: int | None = None
    total_cents: int | None = None
    cash_amount_due_cents: int | None = None
    # Deliberately always None at intake — see module docstring.
    restaurant_net_cents: int | None = None

    special_instructions: str | None = None
    customer_first_name: str | None = None
    customer_phone: str | None = None
    # Uber masks the eater's number behind a proxy line; `phone_code` is the
    # access code to dial with it ("The phone code to access the phone
    # number above") — NOT a country/dialling prefix.
    customer_phone_code: str | None = None
    # Deliberately always None, and typed that way — see module docstring.
    delivery_address: None = None

    fulfillment_issues: tuple[str, ...] = ()

    # Allow-listed, provider-specific extras a future reconciliation job
    # might want (e.g. Uber's `brand`) — built ONLY via
    # uber/mapping.py::build_provider_metadata(), never a raw payload dump.
    provider_metadata: dict = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)


@dataclass(frozen=True)
class CanonicalCatalogueEntry:
    """
    One sellable entity in a provider's menu, read (never written) for the
    mapping screen's bulk import. `kind` is ITEM (sold on its own, listed
    under a category) or MODIFIER (offered inside a modifier group); an
    entity used both ways yields one entry of each kind. `price_cents` is
    the channel's own price, which can legitimately differ from the
    in-store menu price.
    """
    kind: str
    external_id: str
    title: str
    price_cents: int | None = None
    group_title: str | None = None


@dataclass(frozen=True)
class WebhookEnvelope:
    """
    The result of an adapter's PURE, no-network webhook parse — enough to
    look up a `delivery_connections` row and enqueue a worker job, nothing
    more. Deliberately does NOT carry order content (Uber's own
    `orders.notification` envelope doesn't either — see
    `uber/adapter.py::_parse_webhook_event`).
    """
    provider: str
    event_id: str
    event_type: str | None
    external_store_id: str | None
    external_order_id: str | None
    environment: str | None = None


@dataclass
class WebhookGateResult:
    """
    Outcome of the seven ordered trust-boundary gates a webhook must clear
    before any provider-mutating work happens (design doc §4):
    signature -> store resolution -> environment match -> tenant active ->
    entitlement -> connection status -> (idempotent) enqueue.

    `http_status` is ALWAYS 200 for a gate failure — Uber requires an ack,
    and only a genuine internal enqueue failure returns something else, so a
    real paid order is never silently dropped. `error_category` is always
    set on failure so a hostile or misconfigured sender stays visible in
    `delivery_webhook_events` rather than being swallowed.
    """
    accepted: bool
    http_status: int = 200
    error_category: str | None = None
    business_id: uuid.UUID | None = None
    delivery_connection_id: uuid.UUID | None = None
    detail: str | None = None
