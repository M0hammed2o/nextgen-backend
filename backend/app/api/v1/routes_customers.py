"""
Customer list + detail (business-plane) — new functionality, not touching
the WhatsApp bot pipeline or any protected file.

A prior audit found the Manager Dashboard has no customer-list page at all,
for any tenant. These two routes are read-only and strictly scoped by
`user.business_id` from the JWT, matching the exact tenant-isolation
pattern used throughout `routes_orders.py`.

Role requirement mirrors `routes_orders.py`'s own `GET /business/orders`
(`require_staff_or_above`) — customer name/phone/order-history are the same
class of "customer-adjacent" data staff already read on an order, so no new
role requirement is invented here.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_orders import OrderResponse
from backend.app.api.v1.routes_pos import _mask_phone
from backend.app.core.errors import NotFoundError
from backend.app.core.pagination import PaginatedResponse, PaginationMeta
from backend.app.core.rbac import AuthUser, require_staff_or_above
from backend.app.db.session import get_db
from shared.models.customer import Customer
from shared.models.order import Order

router = APIRouter(prefix="/business/customers", tags=["customers"])


def _masked_phone(customer: Customer) -> str | None:
    """Reuses routes_pos.py's existing `_mask_phone` helper rather than
    inventing a second masking scheme. Never the raw number — this is a
    list/detail view, not an order a staff member needs to call."""
    return _mask_phone(customer.phone_number) or _mask_phone(customer.wa_id)


def _display_name(customer: Customer) -> str:
    """Customer's own captured name if present, else the masked phone/wa_id
    above."""
    if customer.display_name:
        return customer.display_name
    return _masked_phone(customer) or "Unknown customer"


class CustomerListItem(BaseModel):
    id: uuid.UUID
    display_name: str
    # Masked (never raw) — same reasoning as display_name's own fallback.
    # Present as its own field (not just folded into display_name) so a
    # caller that already has a real display_name can still show a contact
    # hint alongside it.
    phone_number: str | None = None
    order_count: int
    total_spent_cents: int
    last_order_at: "str | None" = None


class CustomerDetailResponse(BaseModel):
    id: uuid.UUID
    display_name: str
    phone_number: str | None = None
    order_count: int
    total_spent_cents: int
    last_order_at: "str | None" = None
    orders: list[OrderResponse] = []


@router.get("", response_model=PaginatedResponse)
async def list_customers(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    search: str | None = None,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated customer list, scoped strictly by user.business_id (JWT) —
    never client input. `order_count`/`total_spent_cents`/`last_order_at`
    are computed from this tenant's own `orders` rows, one aggregated
    query, not N+1 per customer.
    """
    agg = (
        select(
            Order.customer_id,
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total_cents), 0).label("total_spent_cents"),
            func.max(Order.created_at).label("last_order_at"),
        )
        .where(Order.business_id == user.business_id, Order.customer_id.isnot(None))
        .group_by(Order.customer_id)
        .subquery()
    )

    query = (
        select(Customer, agg.c.order_count, agg.c.total_spent_cents, agg.c.last_order_at)
        .outerjoin(agg, agg.c.customer_id == Customer.id)
        .where(Customer.business_id == user.business_id)
    )
    if search and search.strip():
        term = f"%{search.strip()[:255]}%"
        query = query.where(
            (Customer.display_name.ilike(term))
            | (Customer.phone_number.ilike(term))
            | (Customer.wa_id.ilike(term))
        )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one() or 0

    query = query.order_by(agg.c.last_order_at.desc().nullslast(), Customer.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(query)).all()

    data = [
        CustomerListItem(
            id=customer.id,
            display_name=_display_name(customer),
            phone_number=_masked_phone(customer),
            order_count=int(order_count or 0),
            total_spent_cents=int(total_spent_cents or 0),
            last_order_at=last_order_at.isoformat() if last_order_at else None,
        )
        for customer, order_count, total_spent_cents, last_order_at in rows
    ]

    total_pages = (int(total) + per_page - 1) // per_page if total else 0
    return PaginatedResponse(
        data=data,
        pagination=PaginationMeta(total=int(total), page=page, per_page=per_page, total_pages=total_pages),
    )


@router.get("/{customer_id}", response_model=CustomerDetailResponse)
async def get_customer(
    customer_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Customer detail + order history — both scoped by user.business_id,
    never client input, so a customer_id belonging to another tenant 404s
    exactly like a non-existent one."""
    customer = (
        await db.execute(
            select(Customer).where(
                Customer.id == customer_id, Customer.business_id == user.business_id,
            )
        )
    ).scalar_one_or_none()
    if not customer:
        raise NotFoundError("Customer", str(customer_id))

    orders_result = await db.execute(
        select(Order)
        .where(Order.business_id == user.business_id, Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
    )
    orders = list(orders_result.scalars().all())

    order_count = len(orders)
    total_spent_cents = sum(o.total_cents for o in orders)
    last_order_at = orders[0].created_at.isoformat() if orders else None

    return CustomerDetailResponse(
        id=customer.id,
        display_name=_display_name(customer),
        phone_number=_masked_phone(customer),
        order_count=order_count,
        total_spent_cents=total_spent_cents,
        last_order_at=last_order_at,
        orders=[OrderResponse.model_validate(o) for o in orders],
    )
