"""
Uber Eats <-> canonical mapping. PURE functions — no network, no database,
no SQLAlchemy — same discipline as `external_orders/adapters.py`. This is
the seam every unit test drives.

UNDERSPECIFIED FIELDS, NAMED EXPLICITLY
-------------------------------------------
The task brief's confirmed field list did not include a top-level
`event_type` key on the `orders.notification` webhook envelope, even though
several sibling event NAMES were given (`orders.cancel`, `store.status.
changed`, ...). This module assumes the conventional Uber Eats webhook
envelope shape carries the event name in a top-level `event_type` field,
consistent with every other event name being described as a distinct
"event type" — but this specific field name is an ASSUMPTION, not a
confirmed fact, and should be verified against a real captured payload (or
Uber's webhook-payload reference, which is separate from the endpoint docs
consulted for this change) before this adapter processes live traffic.
Everything downstream degrades safely if the assumption is wrong: an event
with `event_type=None` still resolves its tenant and gets enqueued/acked
identically — only the `delivery_webhook_events.event_type` audit column
and any future event-type-specific branching would be affected.

Similarly, the exact nesting of `selected_modifier_groups` inside a cart
item, and whether `payment.charges` values are plain integers or
`{"amount": ...}` objects, were not given verbatim. `_cents()` below
accepts both shapes defensively; modifier-group parsing walks the most
commonly documented Uber Eats shape (`selected_modifier_groups[].
selected_items[]`) and skips anything that doesn't match rather than
raising, since a malformed/renamed field here must degrade to
"unresolved modifier for a human to look at," never a crash or a guess.
"""

from __future__ import annotations

import hmac
import hashlib
from datetime import datetime, timezone

from backend.app.delivery.canonical import (
    CanonicalDeliveryLine,
    CanonicalDeliveryModifier,
    CanonicalDeliveryOrder,
    DeliveryOrderStatus,
    WebhookEnvelope,
)

# ── Signature verification ───────────────────────────────────────────────────


def compute_signature(raw_body: bytes, client_secret: str) -> str:
    """
    Lowercase hex HMAC-SHA256 of the RAW body, keyed by the app client
    secret — confirmed shape, no "sha256=" prefix (unlike Meta's).
    """
    return hmac.new(client_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header_value: str | None, client_secret: str) -> bool:
    if not header_value or not client_secret:
        return False
    expected = compute_signature(raw_body, client_secret)
    return hmac.compare_digest(expected, header_value.strip().lower())


# ── Webhook envelope parsing ─────────────────────────────────────────────────


def parse_webhook_envelope(payload: dict) -> WebhookEnvelope | None:
    """
    Pure parse of the webhook envelope. Confirmed fields:
    `event_id`, `event_time`, `meta.user_id` (= store id, THE tenant key),
    `meta.resource_id` (= order id), `meta.status`. `resource_href` is
    intentionally never read here (or anywhere else) — see module and
    `base.py` docstrings on the SSRF-closing rule.

    Returns None if the payload doesn't even have an `event_id` — nothing
    usable to log or act on.
    """
    if not isinstance(payload, dict):
        return None
    event_id = payload.get("event_id")
    if not event_id:
        return None
    meta = payload.get("meta") or {}
    return WebhookEnvelope(
        provider="UBER_EATS",
        event_id=str(event_id),
        # See module docstring — this key name is an assumption.
        event_type=payload.get("event_type"),
        external_store_id=str(meta.get("user_id")) if meta.get("user_id") else None,
        external_order_id=str(meta.get("resource_id")) if meta.get("resource_id") else None,
    )


# ── Order-details parsing ────────────────────────────────────────────────────

_CURRENT_STATE_MAP: dict[str, DeliveryOrderStatus] = {
    "CREATED": DeliveryOrderStatus.RECEIVED,
    "ACCEPTED": DeliveryOrderStatus.ACCEPTED,
    "DENIED": DeliveryOrderStatus.DENIED,
    "FINISHED": DeliveryOrderStatus.FULFILLED,
    "CANCELED": DeliveryOrderStatus.CANCELLED,
    "UNKNOWN": DeliveryOrderStatus.UNKNOWN,
}

#: Allow-list for `CanonicalDeliveryOrder.provider_metadata` — copied field
#: by field, never a raw-payload passthrough. Nothing here is eater PII.
_METADATA_ALLOW_LIST = ("brand", "type", "current_state")


def map_current_state(raw_state: str | None) -> DeliveryOrderStatus:
    """Pure mapping of Uber's current_state -> canonical status. Unrecognised -> UNKNOWN."""
    if not raw_state:
        return DeliveryOrderStatus.UNKNOWN
    return _CURRENT_STATE_MAP.get(str(raw_state).upper(), DeliveryOrderStatus.UNKNOWN)


def _cents(node) -> int | None:
    """
    Money fields in Uber's docs are described only as "minor units" without
    a fully confirmed nesting for every field (see module docstring) — this
    accepts a bare int/None, or a dict carrying `amount`.
    """
    if node is None:
        return None
    if isinstance(node, bool):
        return None
    if isinstance(node, int):
        return node
    if isinstance(node, dict):
        amount = node.get("amount")
        return amount if isinstance(amount, int) else None
    return None


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            text = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _parse_modifiers(item: dict) -> tuple[CanonicalDeliveryModifier, ...]:
    modifiers: list[CanonicalDeliveryModifier] = []
    for group in item.get("selected_modifier_groups") or []:
        if not isinstance(group, dict):
            continue
        for selected in group.get("selected_items") or []:
            if not isinstance(selected, dict):
                continue
            title = selected.get("title") or selected.get("name")
            if not title:
                continue
            modifiers.append(
                CanonicalDeliveryModifier(
                    external_id=(
                        str(selected.get("external_data") or selected.get("id"))
                        if (selected.get("external_data") or selected.get("id"))
                        else None
                    ),
                    title=str(title),
                    quantity=int(selected.get("quantity") or 1),
                    price_cents=_cents(selected.get("price")),
                )
            )
    return tuple(modifiers)


def parse_order_details(raw: dict, *, external_store_id: str) -> CanonicalDeliveryOrder:
    """
    Map a raw Get Order Details response to `CanonicalDeliveryOrder`.

    `external_store_id` is passed in explicitly (from the ALREADY-resolved
    `delivery_connections` row) rather than trusted from anywhere in `raw`
    — the response's own `store` object is never used for tenant purposes,
    only for the allow-listed `brand` metadata field.
    """
    cart = raw.get("cart") or {}
    payment = raw.get("payment") or {}
    charges = payment.get("charges") or {}
    eater = raw.get("eater") or {}

    lines: list[CanonicalDeliveryLine] = []
    for item in cart.get("items") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "Unknown item"
        lines.append(
            CanonicalDeliveryLine(
                external_item_id=(
                    str(item.get("external_data") or item.get("id"))
                    if (item.get("external_data") or item.get("id"))
                    else None
                ),
                title=str(title),
                quantity=int(item.get("quantity") or 1),
                unit_price_cents=_cents(item.get("price")),
                modifiers=_parse_modifiers(item),
                special_instructions=item.get("special_instructions"),
            )
        )

    fulfillment_issues = tuple(
        str(issue.get("type") or issue.get("description") or issue)
        for issue in (cart.get("fulfillment_issues") or [])
        if issue
    )

    customer_phone = None
    if eater.get("phone"):
        code = eater.get("phone_code")
        customer_phone = f"{code}{eater.get('phone')}" if code else str(eater.get("phone"))

    provider_metadata = {k: raw.get(k) for k in _METADATA_ALLOW_LIST if raw.get(k) is not None}

    return CanonicalDeliveryOrder(
        provider="UBER_EATS",
        external_order_id=str(raw.get("id") or ""),
        external_store_id=external_store_id,
        lines=lines,
        display_id=raw.get("display_id"),
        provider_status_raw=raw.get("current_state"),
        order_type=raw.get("type"),
        placed_at=_parse_timestamp(raw.get("placed_at")),
        ready_for_pickup_at=_parse_timestamp(raw.get("estimated_ready_for_pickup_at")),
        currency="ZAR",
        subtotal_cents=_cents(charges.get("sub_total")),
        tax_cents=_cents(charges.get("tax")),
        fee_cents=_cents(charges.get("total_fee")),
        total_cents=_cents(charges.get("total")),
        cash_amount_due_cents=_cents(charges.get("cash_amount_due")),
        restaurant_net_cents=None,  # never inferred — see canonical.py docstring
        special_instructions=cart.get("special_instructions"),
        customer_first_name=eater.get("first_name"),
        customer_phone=customer_phone,
        delivery_address=None,
        fulfillment_issues=fulfillment_issues,
        provider_metadata=provider_metadata,
    )
