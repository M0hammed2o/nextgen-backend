"""
THE trust boundary for every inbound delivery webhook.

A webhook has no JWT. This module is the ONLY sanctioned path to a
`business_id` for one — every other module in `backend/app/delivery/` must
go through `resolve_delivery_connection()` rather than ever reading
`business_id` off a payload, header, query string, or URL path directly.
See `docs/DELIVERY_PLATFORM_ARCHITECTURE.md` §4 and
`shared/models/delivery.py`'s module docstring.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.delivery import DeliveryConnection


async def resolve_delivery_connection(
    db: AsyncSession, *, provider: str, external_store_id: str | None
) -> DeliveryConnection | None:
    """
    Look up `(provider, external_store_id)` against `delivery_connections` —
    backed by that table's partial UNIQUE index, so this can never return
    more than one row. Returns None for an unknown store (a restaurant that
    isn't our customer — see `entitlements.py` for what callers must do
    with that, which is "log and otherwise ignore," never a deny).
    """
    if not provider or not external_store_id:
        return None
    result = await db.execute(
        select(DeliveryConnection).where(
            DeliveryConnection.provider == provider,
            DeliveryConnection.external_store_id == external_store_id,
        )
    )
    return result.scalar_one_or_none()
