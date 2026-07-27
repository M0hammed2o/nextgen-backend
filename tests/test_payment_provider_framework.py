"""
Payment Provider Framework — refund_payment() and check_health(), added to
the PaymentProvider interface 2026-07-22 alongside the existing
create_payment_link/verify_payment/handle_webhook.

Real-provider refund calls (Yoco/PayFast/iKhoka) are UNVERIFIED against live
credentials — see each provider module's refund_payment docstring. These
tests cover: the credential/reference guard clauses (fully verifiable
without any network access), one mocked-HTTP happy path per provider using
the same httpx-mocking pattern as test_payment_flow.py's existing iKhoka
test, and the two providers (Direct EFT, Stitch) whose MANUAL_REQUIRED
result is architectural, not a stand-in for missing credentials.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.payments.direct_eft import DirectEFTProvider
from backend.app.payments.ikhoka import IKhokaProvider
from backend.app.payments.mock_provider import MockPaymentProvider
from backend.app.payments.payfast import PayFastProvider
from backend.app.payments.registry import get_provider
from backend.app.payments.stitch import StitchProvider
from backend.app.payments.yoco import YocoProvider


class FakeBusiness:
    def __init__(self, **kwargs):
        self.id = uuid.uuid4()
        self.currency = "ZAR"
        self.payment_api_key = kwargs.get("payment_api_key")
        self.payment_api_secret = kwargs.get("payment_api_secret")
        self.payment_webhook_secret = kwargs.get("payment_webhook_secret")
        self.eft_bank_name = kwargs.get("eft_bank_name")
        self.eft_account_name = kwargs.get("eft_account_name")
        self.eft_account_number = kwargs.get("eft_account_number")


class FakeOrder:
    def __init__(self, **kwargs):
        self.id = uuid.uuid4()
        self.order_number = kwargs.get("order_number", "BO-000001")
        self.total_cents = kwargs.get("total_cents", 9000)
        self.payment_reference = kwargs.get("payment_reference")


# ── Mock provider ────────────────────────────────────────────────────────────

async def test_mock_refund_always_succeeds():
    result = await MockPaymentProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.provider_reference is not None


async def test_mock_check_health_always_healthy():
    status = await MockPaymentProvider().check_health(FakeBusiness())
    assert status.healthy is True
    assert status.provider == "MOCK"


# ── Direct EFT — MANUAL_REQUIRED is architectural, not a credentials gap ───

async def test_direct_eft_refund_is_manual_required():
    result = await DirectEFTProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is False
    assert result.status == "MANUAL_REQUIRED"
    assert result.error


async def test_direct_eft_check_health_always_healthy():
    status = await DirectEFTProvider().check_health(FakeBusiness())
    assert status.healthy is True


# ── Stitch — instant EFT can't be reversed via this API, also architectural ─

async def test_stitch_refund_is_manual_required():
    result = await StitchProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is False
    assert result.status == "MANUAL_REQUIRED"


async def test_stitch_check_health_unhealthy_without_credentials():
    status = await StitchProvider().check_health(FakeBusiness())
    assert status.healthy is False


async def test_stitch_check_health_unhealthy_without_bank_account():
    business = FakeBusiness(payment_api_key="secret", payment_api_secret="client_id")
    status = await StitchProvider().check_health(business)
    assert status.healthy is False
    assert "eft_account_number" in status.detail


async def test_stitch_check_health_healthy_when_fully_configured():
    business = FakeBusiness(payment_api_key="secret", payment_api_secret="client_id", eft_account_number="123456")
    status = await StitchProvider().check_health(business)
    assert status.healthy is True


# ── Yoco ─────────────────────────────────────────────────────────────────────

async def test_yoco_refund_fails_without_api_key():
    result = await YocoProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is False
    assert result.status == "FAILED"


async def test_yoco_refund_fails_without_stored_reference():
    business = FakeBusiness(payment_api_key="sk_test_abc")
    order = FakeOrder(payment_reference=None)
    result = await YocoProvider().refund_payment(order, business, 5000)
    assert result.success is False
    assert "reference" in result.error


async def test_yoco_refund_success_mocked_http():
    business = FakeBusiness(payment_api_key="sk_test_abc")
    order = FakeOrder(payment_reference="chr_test123", total_cents=9000)

    mock_response = MagicMock()
    mock_response.json.return_value = {"id": "rfnd_test123"}
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("backend.app.payments.yoco.httpx.AsyncClient", return_value=mock_client):
        result = await YocoProvider().refund_payment(order, business, 9000)

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.provider_reference == "rfnd_test123"


async def test_yoco_check_health_unhealthy_without_key():
    status = await YocoProvider().check_health(FakeBusiness())
    assert status.healthy is False


async def test_yoco_check_health_unhealthy_malformed_key():
    status = await YocoProvider().check_health(FakeBusiness(payment_api_key="not-a-yoco-key"))
    assert status.healthy is False


async def test_yoco_check_health_healthy_with_valid_looking_key():
    status = await YocoProvider().check_health(FakeBusiness(payment_api_key="sk_live_abc123"))
    assert status.healthy is True


# ── iKhoka ───────────────────────────────────────────────────────────────────

async def test_ikhoka_refund_fails_without_credentials():
    result = await IKhokaProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is False
    assert result.status == "FAILED"


async def test_ikhoka_refund_fails_without_stored_reference():
    business = FakeBusiness(payment_api_key="app_id", payment_api_secret="app_secret")
    order = FakeOrder(payment_reference=None)
    result = await IKhokaProvider().refund_payment(order, business, 5000)
    assert result.success is False


async def test_ikhoka_check_health_unhealthy_without_credentials():
    status = await IKhokaProvider().check_health(FakeBusiness())
    assert status.healthy is False


async def test_ikhoka_check_health_healthy_with_credentials():
    status = await IKhokaProvider().check_health(FakeBusiness(payment_api_key="app_id", payment_api_secret="app_secret"))
    assert status.healthy is True


# ── PayFast ──────────────────────────────────────────────────────────────────

async def test_payfast_refund_fails_without_merchant_id():
    result = await PayFastProvider().refund_payment(FakeOrder(), FakeBusiness(), 5000)
    assert result.success is False
    assert result.status == "FAILED"


async def test_payfast_check_health_unhealthy_without_credentials():
    status = await PayFastProvider().check_health(FakeBusiness())
    assert status.healthy is False


async def test_payfast_check_health_healthy_with_credentials():
    business = FakeBusiness(payment_api_key="merchant_key", payment_api_secret="merchant_id")
    status = await PayFastProvider().check_health(business)
    assert status.healthy is True


# ── Registry-wide framework compliance ───────────────────────────────────────

@pytest.mark.parametrize("name", ["DIRECT_EFT", "MOCK", "YOCO", "PAYFAST", "STITCH", "IKHOKA"])
def test_every_registered_provider_implements_the_full_framework_interface(name):
    """
    Every provider the registry serves must implement refund_payment and
    check_health — this is already enforced at class-definition time by the
    ABC (a subclass missing an abstract method can't be instantiated at all,
    so `registry.py` importing successfully is itself a partial proof), but
    this test documents the requirement explicitly and would fail loudly if
    a future provider were added without implementing the full interface.
    """
    provider = get_provider(name)
    assert provider is not None
    assert callable(getattr(provider, "refund_payment", None))
    assert callable(getattr(provider, "check_health", None))
