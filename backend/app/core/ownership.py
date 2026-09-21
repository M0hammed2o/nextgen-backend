"""
Ownership helper — the platform's core tenant-isolation check, in one place.

Several route handlers accept an object id from the client — as a path
param, or (more dangerously) embedded in a request body as a foreign key,
e.g. `category_id`, `supplier_id`, `photo_asset_id` — and use it to build
or link a row without ever confirming the referenced object belongs to the
caller's own business. That is a real, recurring class of tenant-isolation
defect in this codebase (Wave 0A hardening pass, 2026-09).

`require_owned()` is the one place that check should happen: look a row up
by primary key AND `business_id` together, and raise the same 404
(`NotFoundError`) whether the id doesn't exist at all or belongs to another
tenant — a cross-tenant probe must learn nothing about whether the id is
real (SECURITY.md, Tenant Isolation).
"""

import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError


async def require_owned(
    db: AsyncSession,
    model: Any,
    obj_id: uuid.UUID,
    business_id: uuid.UUID,
    *,
    label: str,
    extra_filters: Iterable[Any] = (),
) -> Any:
    """
    Fetch a `model` row by primary key `obj_id`, scoped to `business_id`.

    Raises `NotFoundError` (404) if no row matches — including when
    `obj_id` is a real id that belongs to a different business. The
    response is identical either way, by design: never confirm to a
    caller that another tenant's object exists.

    `extra_filters` lets a caller add conditions such as a soft-delete
    flag (e.g. `(MenuItem.is_deleted == False,)`) without duplicating this
    lookup pattern per call site.
    """
    result = await db.execute(
        select(model).where(
            model.id == obj_id,
            model.business_id == business_id,
            *extra_filters,
        )
    )
    obj = result.scalar_one_or_none()
    if obj is None:
        raise NotFoundError(label, str(obj_id))
    return obj
