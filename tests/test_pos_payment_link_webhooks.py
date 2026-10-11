"""
POS Pay-by-Link — payment-provider-framework and webhook-routing tests.

Covers two things added for POS split-payment pay-by-link (migration
0055_pos_payment_allocations, model shared/models/pos_payment.py):

1. `create_payment_link(order, business, *, amount_cents=None,
   external_reference=None)` — the extended signature that lets a caller
   request a link for a standalone amount/reference BEFORE any Order
   exists (order=None), while leaving the existing order-bound call sites
   (`provider.create_payment_link(order, business)`, used by the
   WhatsApp-order payment flow) completely unaffected.

2. `_mark_intent_paid()` (backend/app/api/v1/routes_payments.py) — the new
   webhook-routing branch that recognises a `"pos_intent:<uuid>"` reference
   (the same free-text order-correlation field every provider already
   echoes back) and marks a `POSPaymentLinkIntent` PAID instead of calling
   `_mark_order_paid`, with the same amount/currency verification and
   tenant-scoping discipline.

No real network calls are made anywhere in this file — httpx is mocked for
every create_payment_link test, and the webhook tests use a FakeDB plus
real provider signature-verification code (matching the pattern already
established in tests/test_payment_webhook_amount_validation.py).
"""
from __future__ import annotations

import json
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.v1.routes_payments import (
    payfast_webhook,
    yoco_webhook,
)
from backend.app.payments.mock_provider import MockPaymentProvider
from backend.app.payments.payfast import PayFastProvider, _build_signature as payfast_sign
from backend.app.payments.yoco import YocoProvider


# ── Fakes (same pattern as test_payment_webhook_amount_validation.py) ───────

class FakeRequest:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0
        self.added = []

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_business(**overrides):
    class Biz:
        pass
    b = Biz()
    b.id = uuid.uuid4()
    b.payment_webhook_secret = None
    b.payment_api_key = None
    b.payment_api_secret = None
    b.currency = "ZAR"
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def make_order(business_id, **overrides):
    class Order:
        pass
    o = Order()
    o.id = uuid.uuid4()
    o.business_id = business_id
    o.order_number = "BO-000001"
    o.total_cents = 9000
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


def make_intent(business_id, **overrides):
    class Intent:
        pass
    i = Intent()
    i.id = uuid.uuid4()
    i.business_id = business_id
    i.amount_cents = 25000  # R250.00
    i.currency = "ZAR"
    i.status = "PENDING"
    i.provider_reference = None
    i.paid_at = None
    i.expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    for k, v in overrides.items():
        setattr(i, k, v)
    return i


def _mock_httpx_client(json_body: dict):
    mock_response = MagicMock()
    mock_response.json.return_value = json_body
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


# ── create_payment_link: order=None (POS intent) path ──────────────────────

async def test_yoco_create_payment_link_order_none_success():
    # Plain (no "enc1:" prefix) values pass through decrypt_credential()
    # unchanged (backend/app/core/crypto.py) — no need to mock it.
    business = make_business(payment_api_key="sk_test_abc")
    intent_ref = "pos_intent:" + str(uuid.uuid4())
    mock_client = _mock_httpx_client({"url": "https://pay.yoco.com/abc"})

    with patch("backend.app.payments.yoco.httpx.AsyncClient", return_value=mock_client):
        url = await YocoProvider().create_payment_link(
            None, business, amount_cents=25000, external_reference=intent_ref,
        )

    assert url == "https://pay.yoco.com/abc"
    sent_json = mock_client.post.call_args.kwargs["json"]
    assert sent_json["amount"] == 25000
    assert sent_json["metadata"]["orderId"] == intent_ref
    assert "orderNumber" not in sent_json["metadata"]


async def test_yoco_create_payment_link_order_none_missing_amount_raises_valueerror():
    business = make_business(payment_api_key="sk_test_abc")
    with pytest.raises(ValueError):
        await YocoProvider().create_payment_link(
            None, business, amount_cents=None, external_reference="pos_intent:x",
        )


async def test_yoco_create_payment_link_order_none_missing_reference_raises_valueerror():
    business = make_business(payment_api_key="sk_test_abc")
    with pytest.raises(ValueError):
        await YocoProvider().create_payment_link(
            None, business, amount_cents=25000, external_reference=None,
        )


async def test_yoco_create_payment_link_order_given_unaffected_regression():
    """Existing order-bound call site output is byte-for-byte unchanged."""
    business = make_business(payment_api_key="sk_test_abc")
    order = make_order(business.id, total_cents=9000, order_number="BO-000042")
    mock_client = _mock_httpx_client({"url": "https://pay.yoco.com/xyz"})

    with patch("backend.app.payments.yoco.httpx.AsyncClient", return_value=mock_client):
        url = await YocoProvider().create_payment_link(order, business)

    assert url == "https://pay.yoco.com/xyz"
    sent_json = mock_client.post.call_args.kwargs["json"]
    assert sent_json["amount"] == 9000
    assert sent_json["metadata"]["orderId"] == str(order.id)
    assert sent_json["metadata"]["orderNumber"] == "BO-000042"


async def test_payfast_create_payment_link_order_none_success():
    # Plain (no "enc1:" prefix) values pass through decrypt_credential()
    # unchanged — no need to mock it. get_settings() uses its real,
    # non-placeholder defaults (BACKEND_PUBLIC_URL etc.) — no network call.
    business = make_business(payment_api_key="mkey", payment_api_secret="mid")
    intent_ref = "pos_intent:" + str(uuid.uuid4())

    url = await PayFastProvider().create_payment_link(
        None, business, amount_cents=25000, external_reference=intent_ref,
    )

    assert url is not None
    parsed = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert parsed["amount"][0] == "250.00"
    assert parsed["m_payment_id"][0] == intent_ref
    assert parsed["item_name"][0] == "POS payment"


async def test_payfast_create_payment_link_order_none_missing_kwarg_raises_valueerror():
    business = make_business(payment_api_key="mkey", payment_api_secret="mid")
    with pytest.raises(ValueError):
        await PayFastProvider().create_payment_link(None, business, amount_cents=25000)


async def test_payfast_create_payment_link_order_given_unaffected_regression():
    business = make_business(payment_api_key="mkey", payment_api_secret="mid")
    order = make_order(business.id, total_cents=9000, order_number="BO-000042")

    url = await PayFastProvider().create_payment_link(order, business)

    parsed = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert parsed["amount"][0] == "90.00"
    assert parsed["m_payment_id"][0] == str(order.id)
    assert parsed["item_name"][0] == "Order BO-000042"


async def test_mock_provider_create_payment_link_order_none_success():
    intent_ref = "pos_intent:" + str(uuid.uuid4())
    url = await MockPaymentProvider().create_payment_link(
        None, make_business(), amount_cents=25000, external_reference=intent_ref,
    )
    assert url == f"https://pay.mock.test/checkout/{intent_ref}"


async def test_mock_provider_create_payment_link_order_none_missing_kwarg_raises_valueerror():
    with pytest.raises(ValueError):
        await MockPaymentProvider().create_payment_link(None, make_business(), amount_cents=25000)


async def test_mock_provider_create_payment_link_order_given_unaffected_regression():
    order = make_order(uuid.uuid4(), order_number="BO-999999")
    url = await MockPaymentProvider().create_payment_link(order, make_business())
    assert url == "https://pay.mock.test/checkout/BO-999999"


async def test_mock_provider_handle_webhook_echoes_amount_and_currency():
    result = await MockPaymentProvider().handle_webhook(
        {"order_id": "x", "reference": "ref1", "amount_cents": 25000, "currency": "ZAR"}
    )
    assert result["amount_cents"] == 25000
    assert result["currency"] == "ZAR"


async def test_mock_provider_handle_webhook_keeps_existing_behavior_without_amount():
    result = await MockPaymentProvider().handle_webhook({"order_id": "x", "reference": "ref1"})
    assert result == {"order_id": "x", "paid": True, "reference": "ref1"}


# ── Webhook routing: Yoco POS intent branch ─────────────────────────────────

def _yoco_pos_body(intent_ref: str, amount: int, currency: str = "ZAR", status: str = "successful"):
    payload = {
        "type": "checkout.completed",
        "payload": {
            "id": "chr_pos1",
            "status": status,
            "metadata": {"orderId": intent_ref},
            "amount": amount,
            "currency": currency,
        },
    }
    return json.dumps(payload).encode("utf-8")


async def test_yoco_webhook_marks_pos_intent_paid_happy_path():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")
    intent_ref = f"pos_intent:{intent.id}"
    body = _yoco_pos_body(intent_ref, amount=25000, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        result = await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert result == {"received": True}
    assert intent.status == "PAID"
    assert intent.provider_reference == "chr_pos1"
    assert intent.paid_at is not None
    assert db.commits == 1


async def test_yoco_webhook_invalid_signature_rejected_before_pos_intent_logic():
    """Signature verification must run before any payload/intent processing."""
    business = make_business()
    intent_ref = f"pos_intent:{uuid.uuid4()}"
    body = _yoco_pos_body(intent_ref, amount=25000, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business)])  # only the business lookup should ever execute

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=False):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "bad"}), db=db)
    assert exc.value.status_code == 400
    assert db.commits == 0


# ── Webhook routing: PayFast POS intent branch ──────────────────────────────

def _payfast_pos_body(intent_ref: str, amount_gross: str, passphrase: str, pf_payment_id: str = "pf_pos1"):
    params = {
        "m_payment_id": intent_ref,
        "payment_status": "COMPLETE",
        "amount_gross": amount_gross,
        "pf_payment_id": pf_payment_id,
    }
    signed = dict(params)
    signed["signature"] = payfast_sign(params, passphrase)
    return urllib.parse.urlencode(signed).encode("utf-8")


async def test_payfast_webhook_marks_pos_intent_paid_happy_path():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        result = await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert result == {"received": True}
    assert intent.status == "PAID"
    assert intent.provider_reference == "pf_pos1"
    assert db.commits == 1


async def test_payfast_webhook_pos_intent_wrong_tenant_rejected():
    """Intent belongs to a different business — tenant-scoped lookup finds nothing."""
    business_a = make_business()
    other_business_id = uuid.uuid4()
    intent_owned_by_b = make_intent(other_business_id, amount_cents=25000)
    intent_ref = f"pos_intent:{intent_owned_by_b.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret")
    db = FakeDB([FakeResult(scalar=business_a), FakeResult(scalar=None)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business_a.id, request=FakeRequest(body), db=db)

    assert intent_owned_by_b.status == "PENDING"  # never touched
    assert db.commits == 0


async def test_payfast_webhook_pos_intent_amount_mismatch_rejected():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")  # expects R250
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "249.00", "secret")  # provider confirms R249
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "PENDING"  # NOT marked paid
    assert db.commits == 0


async def test_payfast_webhook_pos_intent_amount_too_high_rejected():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")  # expects R250
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "600.00", "secret")  # provider confirms R600
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "PENDING"
    assert db.commits == 0


async def test_yoco_webhook_pos_intent_currency_mismatch_rejected():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")
    intent_ref = f"pos_intent:{intent.id}"
    body = _yoco_pos_body(intent_ref, amount=25000, currency="USD")  # wrong currency
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert intent.status == "PENDING"
    assert db.commits == 0


async def test_yoco_webhook_pos_intent_missing_amount_rejected():
    """
    Unlike the order path (which allows a missing amount through on
    signature-only trust for providers whose payload can't carry one),
    POS intents are only ever Yoco/PAYFAST — both of which DO carry an
    amount — so a missing amount here must be treated as unverifiable and
    rejected, never silently accepted.
    """
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR")
    intent_ref = f"pos_intent:{intent.id}"
    body = _yoco_pos_body(intent_ref, amount=25000, currency="ZAR")
    # Simulate a payload that genuinely lacks the amount field by stripping it.
    payload = json.loads(body)
    del payload["payload"]["amount"]
    body = json.dumps(payload).encode("utf-8")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert intent.status == "PENDING"
    assert db.commits == 0


async def test_payfast_webhook_pos_intent_duplicate_delivery_idempotent_same_reference():
    business = make_business()
    intent = make_intent(
        business.id, amount_cents=25000, currency="ZAR", status="PAID",
        provider_reference="pf_pos1", paid_at=datetime.now(timezone.utc),
    )
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret", pf_payment_id="pf_pos1")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "PAID"
    assert db.commits == 0  # idempotent no-op, no re-write


async def test_payfast_webhook_pos_intent_already_consumed_is_noop():
    business = make_business()
    intent = make_intent(
        business.id, amount_cents=25000, currency="ZAR", status="CONSUMED",
        provider_reference="pf_pos1",
    )
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret", pf_payment_id="pf_pos1")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "CONSUMED"  # never resurrected
    assert db.commits == 0


async def test_payfast_webhook_pos_intent_dead_status_not_resurrected():
    business = make_business()
    intent = make_intent(business.id, amount_cents=25000, currency="ZAR", status="EXPIRED")
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "EXPIRED"
    assert db.commits == 0


async def test_payfast_webhook_pos_intent_past_expiry_not_marked_paid_even_while_still_pending():
    """
    Regression for BUG-0128: `expires_at` is otherwise only enforced lazily
    (routes_pos.py's _maybe_expire, which only runs on the next GET/resend/
    cancel) -- a PENDING intent nobody has "looked at" since it went stale
    must still be refused by the one point that actually finalizes money,
    not just left to whichever happens first. A correctly-signed,
    correct-amount webhook arriving after expires_at must transition the
    intent to EXPIRED, never to PAID.
    """
    business = make_business()
    intent = make_intent(
        business.id, amount_cents=25000, currency="ZAR", status="PENDING",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    intent_ref = f"pos_intent:{intent.id}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=intent)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert intent.status == "EXPIRED"
    assert intent.paid_at is None
    assert db.commits == 1  # the EXPIRED transition itself is persisted


async def test_payfast_webhook_invalid_signature_rejected_before_pos_intent_logic():
    business = make_business()
    intent_ref = f"pos_intent:{uuid.uuid4()}"
    body = _payfast_pos_body(intent_ref, "250.00", "secret")
    db = FakeDB([FakeResult(scalar=business)])  # only the business lookup should ever execute

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="WRONG_PASSPHRASE"):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)
    assert exc.value.status_code == 400
    assert db.commits == 0


# ── Regression: existing order-webhook behavior is unaffected ──────────────

async def test_yoco_webhook_normal_order_path_unaffected_by_pos_branch():
    """An ordinary (non-"pos_intent:") order_id must still go to _mark_order_paid."""
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    order.payment_status = "UNPAID"
    order.payment_required = False
    order.customer_id = None
    order.status = "NEW"
    order.payment_reference = None
    order.paid_at = None
    order.currency = "ZAR"
    body = _yoco_pos_body(str(order.id), amount=9000, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        result = await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert result == {"received": True}
    assert order.payment_status == "PAID"
