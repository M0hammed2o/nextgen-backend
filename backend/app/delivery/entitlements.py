"""
Delivery entitlement checks — `Business.delivery_platforms_enabled` (module
master switch) AND the specific provider flag (`uber_eats_enabled` /
`mrd_enabled`) must both be true. See `shared/models/business.py` for why
these are separate from the pre-existing `delivery_enabled` column.
"""

from __future__ import annotations

import uuid

from backend.app.core.config import get_settings
from shared.models.business import Business

PROVIDER_ENTITLEMENT_COLUMN: dict[str, str] = {
    "UBER_EATS": "uber_eats_enabled",
    "MR_D": "mrd_enabled",
}


def is_provider_entitled(business: Business | None, provider: str) -> bool:
    if business is None:
        return False
    if not getattr(business, "delivery_platforms_enabled", False):
        return False
    column = PROVIDER_ENTITLEMENT_COLUMN.get(provider)
    if not column:
        return False
    return bool(getattr(business, column, False))


def is_connection_live(connection) -> bool:
    """
    A connection is "live" (may process a real order) only when CONNECTED.

    Commercial-correctness requirement, not a simplification target: Uber
    Eats production access requires Uber's own written approval, so a
    connection can genuinely be SANDBOX or AWAITING_APPROVAL — wired up,
    but unable to take a single real order — and must be treated exactly
    like NOT_CONNECTED/ATTENTION_REQUIRED/DISABLED here: not live. Only
    CONNECTED may ever be treated as live for real order processing.
    """
    return bool(connection) and connection.status == "CONNECTED"


def sandbox_order_business_ids() -> frozenset[uuid.UUID]:
    """
    Test tenants allowed to book SANDBOX orders as real Orders
    (`UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS`). Malformed entries are ignored
    rather than guessed at — an unparseable id grants nothing.
    """
    raw = get_settings().UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS or ""
    ids: set[uuid.UUID] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(uuid.UUID(part))
        except ValueError:
            continue
    return frozenset(ids)


def is_sandbox_test_connection(connection) -> bool:
    """
    A SANDBOX connection, on the sandbox environment, belonging to a test
    tenant explicitly allow-listed by ops. Sandbox orders are tests: this is
    what lets them run end to end (fetch, map, kitchen, accept) without ever
    letting one land in a real restaurant's books. `is_connection_live`
    deliberately stays False for it — nothing may describe it as live.
    """
    return (
        bool(connection)
        and connection.status == "SANDBOX"
        and connection.environment == "sandbox"
        and connection.business_id in sandbox_order_business_ids()
    )


def may_process_orders(connection) -> bool:
    """True when orders on this connection may become real NextGen Orders."""
    return is_connection_live(connection) or is_sandbox_test_connection(connection)
