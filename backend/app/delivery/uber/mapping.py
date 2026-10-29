"""
Uber Eats <-> canonical mapping. PURE functions — no network, no database,
no SQLAlchemy — same discipline as `external_orders/adapters.py`. This is
the seam every unit test drives.

SOURCE OF TRUTH FOR EVERY SHAPE BELOW
------------------------------------------
Uber's own v2 Get Order Details reference, its webhook references and its
menu payload examples (developer.uber.com/docs/eats, read 2026-10-26).
Earlier revisions of this module guessed three shapes that turned out to be
wrong against those references, all corrected here:

* Item money is NESTED: `price.unit_price.amount` / `price.total_price.amount`
  / `price.base_unit_price.amount` (integer minor units). The old code read
  `price.amount`, which does not exist, so every line priced as None -> R0.
  A flat `{"amount": n}` is still accepted as a fallback, defensively.
* `eater.phone_code` is "the phone code to access the phone number above"
  (an access code for Uber's masked proxy number), NOT a dialling prefix.
  The old code glued it onto the front of the number.
* `payment.charges.total` is "the cost of the entire order, including taxes
  and fees that will be paid out to the Merchant" — it is not the food sale.
  `sub_total` is "the total cost of each line item". See
  `order_service._create_order_from_canonical` for how each is booked.

`event_type` IS a top-level envelope field ("Always check this field as new
event types may be added in the future without notice"), so only the order
lifecycle types listed in `ORDER_EVENT_TYPES` ever start order processing.

MAPPING KEY: THE PROVIDER'S ENTITY `id`
-------------------------------------------
A cart item's `id` is the menu entity's own id (Uber's example cart item
"Muffin" is the menu item "Muffin"), unique within a store's menu and always
present. `external_data` is optional free text a previous integrator may
have set and need not be unique, so it is only a fallback when `id` is
missing. The catalogue import (`parse_menu_catalogue`) keys on the same
`id`, so an imported mapping always matches the orders that follow.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from backend.app.delivery.canonical import (
    CanonicalCatalogueEntry,
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

#: The only event types that create/advance a delivery order. Anything else
#: (store.*, delivery.state_changed, an event type Uber adds later) is
#: logged and acknowledged, never turned into an order — even if it happens
#: to carry a `meta.resource_id`.
ORDER_EVENT_TYPES = frozenset({
    "orders.notification",
    "orders.scheduled.notification",
    "orders.cancel",
    "orders.failure",
    "orders.customer_order_edit",
})


def parse_webhook_envelope(payload: dict) -> WebhookEnvelope | None:
    """
    Pure parse of the webhook envelope. Documented fields: `event_type`,
    `event_id`, `event_time`, `meta.user_id` (= store id, THE tenant key),
    `meta.resource_id` (= order id), `meta.status`. Store events instead
    carry a top-level `store_id`. `resource_href` is intentionally never
    read here (or anywhere else) — see module and `base.py` docstrings on
    the SSRF-closing rule.

    Returns None if the payload doesn't even have an `event_id` — nothing
    usable to log or act on.
    """
    if not isinstance(payload, dict):
        return None
    event_id = payload.get("event_id")
    if not event_id:
        return None
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    event_type = payload.get("event_type")
    event_type = str(event_type) if event_type else None

    store_id = meta.get("user_id") or payload.get("store_id")
    order_id = meta.get("resource_id") if event_type in ORDER_EVENT_TYPES else None
    return WebhookEnvelope(
        provider="UBER_EATS",
        event_id=str(event_id),
        event_type=event_type,
        external_store_id=str(store_id) if store_id else None,
        external_order_id=str(order_id) if order_id else None,
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

#: Uber nests modifiers inside modifiers; real menus stop well short of this.
_MAX_MODIFIER_DEPTH = 3


def map_current_state(raw_state: str | None) -> DeliveryOrderStatus:
    """Pure mapping of Uber's current_state -> canonical status. Unrecognised -> UNKNOWN."""
    if not raw_state:
        return DeliveryOrderStatus.UNKNOWN
    return _CURRENT_STATE_MAP.get(str(raw_state).upper(), DeliveryOrderStatus.UNKNOWN)


def _cents(node) -> int | None:
    """Uber Money is `{amount, currency_code, formatted_amount}`, amount in minor units."""
    if node is None or isinstance(node, bool):
        return None
    if isinstance(node, int):
        return node
    if isinstance(node, dict):
        amount = node.get("amount")
        if isinstance(amount, bool):
            return None
        return amount if isinstance(amount, int) else None
    return None


def _price_cents(price, key: str) -> int | None:
    """`price.<key>.amount`, falling back to a flat `price.amount` if that's all there is."""
    if isinstance(price, dict):
        if key in price:
            return _cents(price.get(key))
        if "amount" in price:
            return _cents(price)
        return None
    return _cents(price)


def _currency(charges: dict) -> str | None:
    for key in ("total", "sub_total"):
        node = charges.get(key)
        if isinstance(node, dict) and node.get("currency_code"):
            return str(node["currency_code"]).upper()[:3]
    return None


#: Uber's epoch timestamps "may be seconds or milliseconds" (Integration Tech
#: Support, 2026-10-29). Seconds stay below this until the year 5138.
_EPOCH_MILLIS_THRESHOLD = 100_000_000_000


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(value) >= _EPOCH_MILLIS_THRESHOLD:
            value = value / 1000
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


def _entity_id(node: dict) -> str | None:
    value = node.get("id") or node.get("external_data")
    return str(value) if value else None


def _quantity(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_modifiers(item: dict, depth: int = 0) -> tuple[CanonicalDeliveryModifier, ...]:
    """
    Every selected modifier (and every removed default ingredient) on an
    item, flattening nested modifier groups. Anything that doesn't match the
    documented shape is skipped rather than raising — a renamed field must
    degrade to "unresolved modifier for a human to look at", never a crash.
    """
    if depth >= _MAX_MODIFIER_DEPTH:
        return ()
    modifiers: list[CanonicalDeliveryModifier] = []
    for group in item.get("selected_modifier_groups") or []:
        if not isinstance(group, dict):
            continue
        group_id = _entity_id(group)
        group_title = group.get("title")
        for selected in group.get("selected_items") or []:
            if not isinstance(selected, dict):
                continue
            title = selected.get("title") or selected.get("name")
            if not title:
                continue
            modifiers.append(CanonicalDeliveryModifier(
                external_id=_entity_id(selected),
                title=str(title),
                quantity=max(_quantity(selected.get("quantity"), 1), 1),
                price_cents=_price_cents(selected.get("price"), "unit_price"),
                group_external_id=group_id,
                group_title=str(group_title) if group_title else None,
            ))
            modifiers.extend(_parse_modifiers(selected, depth + 1))
        for removed in group.get("removed_items") or []:
            if not isinstance(removed, dict):
                continue
            title = removed.get("title") or removed.get("name")
            if not title:
                continue
            modifiers.append(CanonicalDeliveryModifier(
                external_id=_entity_id(removed),
                title=str(title),
                quantity=1,
                price_cents=None,
                removed=True,
                group_external_id=group_id,
                group_title=str(group_title) if group_title else None,
            ))
    return tuple(modifiers)


def _fulfillment_issue_text(issue) -> str:
    if not isinstance(issue, dict):
        return str(issue)
    kind = issue.get("fulfillment_issue_type") or issue.get("type") or "ISSUE"
    root = issue.get("root_item") if isinstance(issue.get("root_item"), dict) else {}
    action = issue.get("fulfillment_action_type") or (
        (root.get("fulfillment_action") or {}).get("fulfillment_action_type")
        if isinstance(root.get("fulfillment_action"), dict) else None
    )
    parts = [str(kind)]
    if root.get("title"):
        parts.append(str(root["title"]))
    if action:
        parts.append(f"({action})")
    return " ".join(parts)


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
        price = item.get("price")
        lines.append(
            CanonicalDeliveryLine(
                external_item_id=_entity_id(item),
                title=str(title),
                quantity=max(_quantity(item.get("quantity"), 1), 1),
                unit_price_cents=_price_cents(price, "unit_price"),
                total_price_cents=_price_cents(price, "total_price"),
                base_unit_price_cents=_price_cents(price, "base_unit_price"),
                modifiers=_parse_modifiers(item),
                special_instructions=item.get("special_instructions") or None,
            )
        )

    fulfillment_issues = tuple(
        _fulfillment_issue_text(issue)
        for issue in (cart.get("fulfillment_issues") or [])
        if issue
    )

    provider_metadata = {k: raw.get(k) for k in _METADATA_ALLOW_LIST if raw.get(k) is not None}

    return CanonicalDeliveryOrder(
        provider="UBER_EATS",
        external_order_id=str(raw.get("id") or ""),
        external_store_id=external_store_id,
        lines=lines,
        display_id=str(raw["display_id"]) if raw.get("display_id") else None,
        provider_status_raw=raw.get("current_state"),
        order_type=raw.get("type"),
        placed_at=_parse_timestamp(raw.get("placed_at")),
        ready_for_pickup_at=_parse_timestamp(raw.get("estimated_ready_for_pickup_at")),
        currency=_currency(charges) or "ZAR",
        subtotal_cents=_cents(charges.get("sub_total")),
        tax_cents=_cents(charges.get("tax")),
        fee_cents=_cents(charges.get("total_fee")),
        total_cents=_cents(charges.get("total")),
        cash_amount_due_cents=_cents(charges.get("cash_amount_due")),
        restaurant_net_cents=None,  # never inferred — see canonical.py docstring
        special_instructions=cart.get("special_instructions") or None,
        customer_first_name=eater.get("first_name"),
        customer_phone=str(eater["phone"]) if eater.get("phone") else None,
        customer_phone_code=str(eater["phone_code"]) if eater.get("phone_code") else None,
        delivery_address=None,
        fulfillment_issues=fulfillment_issues,
        provider_metadata=provider_metadata,
    )


# ── Menu catalogue parsing (read-only; feeds the mapping screen) ─────────────


def _translated(title) -> str | None:
    """Uber menu titles are `{"translations": {"en_us": "..."}}`; take English, else any."""
    if isinstance(title, str):
        return title or None
    if not isinstance(title, dict):
        return None
    translations = title.get("translations")
    if not isinstance(translations, dict) or not translations:
        return None
    for key in ("en_us", "en", "en_za", "en_gb"):
        if translations.get(key):
            return str(translations[key])
    first = next((v for v in translations.values() if v), None)
    return str(first) if first else None


def parse_menu_catalogue(raw: dict) -> list[CanonicalCatalogueEntry]:
    """
    Flatten a v2 Get Menu response (`items`, `categories[].entities`,
    `modifier_groups[].modifier_options`) into catalogue entries. An item
    listed under a category is an ITEM; an item offered in a modifier group
    is a MODIFIER; an item that is both yields both. One entry per
    (kind, id) — a mapping describes the entity, so a modifier offered in
    several groups is listed once, under the first group that offers it.
    Read-only — nothing here ever writes to Uber.
    """
    items = {
        str(i["id"]): i for i in (raw.get("items") or [])
        if isinstance(i, dict) and i.get("id")
    }

    def price_of(item: dict) -> int | None:
        info = item.get("price_info")
        return _cents(info.get("price")) if isinstance(info, dict) else None

    entries: list[CanonicalCatalogueEntry] = []
    seen: set[tuple[str, str]] = set()

    for category in raw.get("categories") or []:
        if not isinstance(category, dict):
            continue
        for entity in category.get("entities") or []:
            item_id = str(entity.get("id")) if isinstance(entity, dict) and entity.get("id") else None
            item = items.get(item_id) if item_id else None
            if not item:
                continue
            key = ("ITEM", item_id)
            if key in seen:
                continue
            seen.add(key)
            entries.append(CanonicalCatalogueEntry(
                kind="ITEM", external_id=item_id,
                title=_translated(item.get("title")) or item_id,
                price_cents=price_of(item),
            ))

    for group in raw.get("modifier_groups") or []:
        if not isinstance(group, dict):
            continue
        group_title = _translated(group.get("title"))
        for option in group.get("modifier_options") or []:
            item_id = str(option.get("id")) if isinstance(option, dict) and option.get("id") else None
            item = items.get(item_id) if item_id else None
            if not item:
                continue
            key = ("MODIFIER", item_id)
            if key in seen:
                continue
            seen.add(key)
            entries.append(CanonicalCatalogueEntry(
                kind="MODIFIER", external_id=item_id,
                title=_translated(item.get("title")) or item_id,
                price_cents=price_of(item),
                group_title=group_title,
            ))

    return entries
