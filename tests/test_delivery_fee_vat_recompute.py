"""
POST /business/orders/{id}/delivery-fee — total recomputed through the
shared VAT/discount path (Wave 0A).

Before this fix, `set_delivery_fee` computed `total = subtotal + fee`
directly, silently discarding `discount_cents` and never applying VAT at
all — for a VAT-registered, exclusive-pricing business that meant the
final total_cents undercharged the customer by the VAT amount the moment a
delivery fee was involved. This proves the fix recomputes through
`shared/pricing/engine.py::apply_vat_to_total`, the same path used at order
creation, preserving any existing discount.
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_orders import DeliveryFeeRequest, set_delivery_fee
from backend.app.core.errors import InvalidTransitionError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.order import Order


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_order(business_id, subtotal_cents=10000, discount_cents=0, status="PENDING_DELIVERY_FEE"):
    order = Order()
    now = datetime.now(timezone.utc)
    for k, v in dict(
        id=uuid.uuid4(), business_id=business_id, order_number="BO-000001",
        status=status, payment_status="UNPAID", payment_method=None,
        payment_required=False, delivery_fee_cents=0, subtotal_cents=subtotal_cents,
        discount_cents=discount_cents, tax_cents=0, total_cents=subtotal_cents,
        currency="ZAR", customer_id=None, order_mode="DELIVERY", source="WHATSAPP",
        created_at=now, updated_at=now,
    ).items():
        setattr(order, k, v)
    order.items = []
    return order


def make_business(business_id, vat_registered=False, vat_rate_basis_points=1500,
                   vat_inclusive_pricing=True, whatsapp_phone_number_id=None, currency="ZAR"):
    import types
    return types.SimpleNamespace(
        id=business_id, vat_registered=vat_registered,
        vat_rate_basis_points=vat_rate_basis_points,
        vat_inclusive_pricing=vat_inclusive_pricing,
        whatsapp_phone_number_id=whatsapp_phone_number_id, currency=currency,
    )


async def _run(order, business, fee_cents, user=None):
    user = user or make_user(business_id=order.business_id)
    db = FakeDB([
        FakeResult(scalar=order),
        FakeResult(scalar=business),
    ])
    result = await set_delivery_fee(
        order_id=order.id, body=DeliveryFeeRequest(delivery_fee_cents=fee_cents),
        user=user, db=db,
    )
    return result, db


# ── Non-VAT business: unchanged arithmetic, but discount now preserved ─────

async def test_non_vat_business_total_is_subtotal_minus_discount_plus_fee():
    order = make_order(uuid.uuid4(), subtotal_cents=10000, discount_cents=1000)
    business = make_business(order.business_id, vat_registered=False)

    result, _ = await _run(order, business, fee_cents=2000)
    assert result.total_cents == 10000 - 1000 + 2000
    assert result.tax_cents == 0
    assert result.delivery_fee_cents == 2000


async def test_no_discount_no_vat_matches_old_behaviour():
    order = make_order(uuid.uuid4(), subtotal_cents=10000, discount_cents=0)
    business = make_business(order.business_id, vat_registered=False)

    result, _ = await _run(order, business, fee_cents=1500)
    assert result.total_cents == 11500


# ── VAT-inclusive: total unchanged by VAT switching on, but tax_cents set ──

async def test_vat_inclusive_total_matches_subtotal_plus_fee_but_tax_cents_recorded():
    order = make_order(uuid.uuid4(), subtotal_cents=10000, discount_cents=0)
    business = make_business(
        order.business_id, vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=True,
    )

    result, _ = await _run(order, business, fee_cents=2000)
    assert result.total_cents == 12000  # inclusive: total unchanged by VAT itself
    assert result.tax_cents > 0  # but the VAT portion is now recorded


# ── VAT-exclusive: this was the real bug — total must GROW by the VAT ──────

async def test_vat_exclusive_business_total_grows_by_vat_amount():
    """
    The bug this closes: `total = subtotal + fee` never added VAT for an
    exclusive-pricing, VAT-registered business — the customer's total was
    silently undercharged by the VAT amount the moment a delivery fee was
    added to a VAT-exclusive order.
    """
    order = make_order(uuid.uuid4(), subtotal_cents=10000, discount_cents=0)
    business = make_business(
        order.business_id, vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=False,
    )

    result, _ = await _run(order, business, fee_cents=2000)
    net = 10000 + 2000
    expected_tax = round(net * 0.15)
    assert result.total_cents == net + expected_tax
    assert result.tax_cents == expected_tax
    assert result.total_cents > net  # the old buggy behaviour would have stopped at `net`


# ── Discount preserved alongside VAT ────────────────────────────────────────

async def test_discount_and_vat_both_applied():
    order = make_order(uuid.uuid4(), subtotal_cents=10000, discount_cents=1000)
    business = make_business(
        order.business_id, vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=False,
    )

    result, _ = await _run(order, business, fee_cents=2000)
    net = 10000 - 1000 + 2000
    expected_tax = round(net * 0.15)
    assert result.total_cents == net + expected_tax


# ── Existing guards unaffected ──────────────────────────────────────────────

async def test_wrong_status_is_rejected():
    order = make_order(uuid.uuid4(), status="NEW")
    db = FakeDB([FakeResult(scalar=order)])
    user = make_user(business_id=order.business_id)

    with pytest.raises(InvalidTransitionError):
        await set_delivery_fee(
            order_id=order.id, body=DeliveryFeeRequest(delivery_fee_cents=1000), user=user, db=db,
        )


async def test_unknown_order_is_404():
    db = FakeDB([FakeResult(scalar=None)])
    user = make_user()
    with pytest.raises(NotFoundError):
        await set_delivery_fee(
            order_id=uuid.uuid4(), body=DeliveryFeeRequest(delivery_fee_cents=1000), user=user, db=db,
        )


async def test_missing_business_is_404():
    order = make_order(uuid.uuid4())
    user = make_user(business_id=order.business_id)
    db = FakeDB([FakeResult(scalar=order), FakeResult(scalar=None)])
    with pytest.raises(NotFoundError):
        await set_delivery_fee(
            order_id=order.id, body=DeliveryFeeRequest(delivery_fee_cents=1000), user=user, db=db,
        )
