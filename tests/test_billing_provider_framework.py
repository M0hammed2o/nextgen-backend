"""
BillingProvider abstraction (backend/app/billing/provider_base.py) —
StripeBillingProvider, PayFastBillingProvider, and the registry that
selects between them by config. PayFast's HTTP calls are UNVERIFIED against
live credentials (same disclosure as the customer-payment framework) — these
tests cover the credential/token guard clauses and one mocked-HTTP path,
not a confirmed-correct live integration.
"""
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.billing.payfast_billing import PayFastBillingProvider
from backend.app.billing.provider_base import BillingCancelResult
from backend.app.billing.registry import get_billing_provider
from backend.app.billing.stripe_billing import StripeBillingProvider
from backend.app.core.errors import AppError


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(), stripe_subscription_id=None, payfast_subscription_token=None,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ── Registry ─────────────────────────────────────────────────────────────────

def test_registry_defaults_to_stripe():
    with patch("backend.app.billing.registry.get_settings") as mock_settings:
        mock_settings.return_value.BILLING_PROVIDER = "STRIPE"
        provider = get_billing_provider()
    assert isinstance(provider, StripeBillingProvider)


def test_registry_selects_payfast_when_configured():
    with patch("backend.app.billing.registry.get_settings") as mock_settings:
        mock_settings.return_value.BILLING_PROVIDER = "PAYFAST"
        provider = get_billing_provider()
    assert isinstance(provider, PayFastBillingProvider)


def test_registry_rejects_unknown_provider_name():
    with patch("backend.app.billing.registry.get_settings") as mock_settings:
        mock_settings.return_value.BILLING_PROVIDER = "SOMETHING_ELSE"
        with pytest.raises(ValueError):
            get_billing_provider()


# ── StripeBillingProvider.cancel_subscription ────────────────────────────────

async def test_stripe_cancel_fails_without_subscription_id():
    result = await StripeBillingProvider().cancel_subscription(db=None, business=make_business())
    assert result.success is False
    assert result.status == "FAILED"


async def test_stripe_cancel_succeeds_with_mocked_sdk():
    business = make_business(stripe_subscription_id="sub_123")
    fake_stripe = MagicMock()
    fake_stripe.Subscription.cancel = MagicMock()
    with patch.dict("sys.modules", {"stripe": fake_stripe}):
        result = await StripeBillingProvider().cancel_subscription(db=None, business=business)
    assert result.success is True
    assert result.status == "COMPLETED"


# ── PayFastBillingProvider ───────────────────────────────────────────────────

async def test_payfast_checkout_fails_when_billing_credentials_not_configured():
    with patch("backend.app.billing.payfast_billing.settings") as mock_settings:
        mock_settings.PAYFAST_BILLING_MERCHANT_ID = ""
        mock_settings.PAYFAST_BILLING_MERCHANT_KEY = ""
        with pytest.raises(AppError) as exc:
            await PayFastBillingProvider().create_checkout_session(db=None, business=make_business(), plan="STARTER")
    assert exc.value.code == "BILLING_PROVIDER_NOT_CONFIGURED"


async def test_payfast_checkout_builds_url_when_configured():
    with patch("backend.app.billing.payfast_billing.settings") as mock_settings:
        mock_settings.PAYFAST_BILLING_MERCHANT_ID = "10000100"
        mock_settings.PAYFAST_BILLING_MERCHANT_KEY = "46f0cd694581a"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = "test-passphrase"
        mock_settings.BILLING_SUCCESS_URL = "https://app.example.com/billing?ok"
        mock_settings.BILLING_CANCEL_URL = "https://app.example.com/billing?cancelled"
        mock_settings.BACKEND_PUBLIC_URL = "https://api.example.com"

        url = await PayFastBillingProvider().create_checkout_session(db=None, business=make_business(), plan="STARTER")

    assert url.startswith("https://www.payfast.co.za/eng/process?")
    assert "signature=" in url
    assert "subscription_type=1" in url


async def test_payfast_checkout_rejects_unmapped_plan():
    with patch("backend.app.billing.payfast_billing.settings") as mock_settings:
        mock_settings.PAYFAST_BILLING_MERCHANT_ID = "10000100"
        mock_settings.PAYFAST_BILLING_MERCHANT_KEY = "46f0cd694581a"
        with pytest.raises(AppError) as exc:
            await PayFastBillingProvider().create_checkout_session(db=None, business=make_business(), plan="ENTERPRISE")
    assert exc.value.code == "PLAN_NOT_SELF_SERVICE"


async def test_payfast_has_no_self_service_portal():
    provider = PayFastBillingProvider()
    assert provider.supports_self_service_portal() is False
    assert await provider.create_portal_session(make_business()) is None


async def test_payfast_cancel_fails_without_subscription_token():
    result = await PayFastBillingProvider().cancel_subscription(db=None, business=make_business())
    assert result.success is False
    assert result.status == "FAILED"


async def test_payfast_cancel_success_mocked_http():
    business = make_business(payfast_subscription_token="pf_token_abc")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.put = AsyncMock(return_value=mock_response)

    with patch("backend.app.billing.payfast_billing.httpx.AsyncClient", return_value=mock_client), \
         patch("backend.app.billing.payfast_billing.settings") as mock_settings:
        mock_settings.PAYFAST_BILLING_MERCHANT_ID = "10000100"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = "test-passphrase"
        result = await PayFastBillingProvider().cancel_subscription(db=None, business=business)

    assert result.success is True
    assert result.status == "COMPLETED"
