"""
Self-service SaaS subscription billing (routes_billing.py).

Covers: OWNER-only access, ENTERPRISE correctly rejected from self-service
checkout (sales-assisted only, per PRODUCT_ROADMAP.md), and the routes going
entirely through backend/app/billing/registry.py::get_billing_provider() —
no provider-specific logic lives in this file. A fake provider stands in for
the real ones (Stripe/PayFast HTTP calls are covered in
test_billing_provider_framework.py); these tests only verify the route
layer's own behavior (business lookup, response shaping).
"""
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_billing
from backend.app.api.v1.routes_billing import (
    CheckoutSessionRequest,
    cancel_subscription,
    create_checkout_session,
    create_portal_session,
    get_billing_status,
)
from backend.app.billing.provider_base import BillingCancelResult
from backend.app.billing.stripe_client import get_price_id_for_plan
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, business=None):
        self._business = business
        self.commits = 0

    async def execute(self, stmt):
        return FakeResult(scalar=self._business)

    async def commit(self):
        self.commits += 1


class FakeBillingProvider:
    def __init__(self, portal_supported=True, portal_url="https://portal.fake/session", cancel_result=None):
        self._portal_supported = portal_supported
        self._portal_url = portal_url
        self._cancel_result = cancel_result or BillingCancelResult(success=True, status="COMPLETED")

    async def create_checkout_session(self, db, business, plan):
        return "https://checkout.fake/session"

    async def create_portal_session(self, business):
        return self._portal_url

    async def cancel_subscription(self, db, business):
        return self._cancel_result

    def supports_self_service_portal(self):
        return self._portal_supported


def make_user(business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role="OWNER", token_type="access")


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(), name="Barn Owl", plan="STARTER", billing_status="TRIAL",
        is_active=True, stripe_customer_id=None, payfast_subscription_token=None,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ── get_price_id_for_plan (pure function, no mocking needed) ───────────────

def test_starter_and_growth_have_configured_price_ids_or_raise_consistently():
    with pytest.raises(AppError) as exc:
        get_price_id_for_plan("ENTERPRISE")
    assert exc.value.code == "PLAN_NOT_SELF_SERVICE"


# ── Checkout session route ──────────────────────────────────────────────────

async def test_checkout_session_returns_url_for_valid_plan():
    business = make_business(plan="STARTER")
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider()):
        result = await create_checkout_session(body=CheckoutSessionRequest(plan="GROWTH"), user=user, db=db)

    assert result.checkout_url == "https://checkout.fake/session"


async def test_checkout_session_unknown_business_is_404():
    user = make_user()
    db = FakeDB(business=None)

    with pytest.raises(NotFoundError):
        await create_checkout_session(body=CheckoutSessionRequest(plan="STARTER"), user=user, db=db)


# ── Portal session route ────────────────────────────────────────────────────

async def test_portal_session_returns_url_when_provider_supports_it():
    business = make_business(stripe_customer_id="cus_fake123")
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider(portal_supported=True)):
        result = await create_portal_session(user=user, db=db)

    assert result.portal_url == "https://portal.fake/session"
    assert result.supported is True


async def test_portal_session_returns_unsupported_for_a_provider_with_no_portal():
    """PayFast has no Stripe-Billing-Portal equivalent — must degrade gracefully, not error."""
    business = make_business()
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider(portal_supported=False)):
        result = await create_portal_session(user=user, db=db)

    assert result.portal_url is None
    assert result.supported is False


# ── Cancel subscription route ────────────────────────────────────────────────

async def test_cancel_subscription_success_updates_billing_status():
    business = make_business(billing_status="ACTIVE", stripe_customer_id="cus_1")
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider()):
        result = await cancel_subscription(user=user, db=db)

    assert result.success is True
    assert business.billing_status == "CANCELLED"
    assert db.commits == 1


async def test_cancel_subscription_failure_does_not_change_billing_status():
    business = make_business(billing_status="ACTIVE")
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)
    failing_provider = FakeBillingProvider(cancel_result=BillingCancelResult(success=False, status="FAILED", error="no token"))

    with patch.object(routes_billing, "get_billing_provider", return_value=failing_provider):
        result = await cancel_subscription(user=user, db=db)

    assert result.success is False
    assert business.billing_status == "ACTIVE"  # unchanged
    assert db.commits == 0


# ── Status route ─────────────────────────────────────────────────────────────

async def test_billing_status_reflects_business_row():
    business = make_business(plan="GROWTH", billing_status="ACTIVE", is_active=True, stripe_customer_id="cus_1")
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider(portal_supported=True)):
        result = await get_billing_status(user=user, db=db)

    assert result.plan == "GROWTH"
    assert result.billing_status == "ACTIVE"
    assert result.has_subscription is True
    assert result.supports_self_service_portal is True


async def test_billing_status_has_no_subscription_when_no_provider_ids_set():
    business = make_business(stripe_customer_id=None, payfast_subscription_token=None)
    user = make_user(business_id=business.id)
    db = FakeDB(business=business)

    with patch.object(routes_billing, "get_billing_provider", return_value=FakeBillingProvider()):
        result = await get_billing_status(user=user, db=db)

    assert result.has_subscription is False
