"""
Delivery entitlement checks — `Business.delivery_platforms_enabled` (module
master switch) AND the specific provider flag (`uber_eats_enabled` /
`mrd_enabled`) must both be true. See `shared/models/business.py` for why
these are separate from the pre-existing `delivery_enabled` column.
"""

from __future__ import annotations

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
    """A connection is "live" only when ACTIVE — PENDING/PAUSED/DISCONNECTED/ERROR are not."""
    return bool(connection) and connection.status == "ACTIVE"
