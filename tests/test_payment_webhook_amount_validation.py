"""
Fix group 3 (wave0a/sec) — tenant payment webhook amount/currency validation.

_mark_order_paid() (routes_payments.py) previously marked an order PAID on
the provider's say-so alone, with no check that the confirmed amount/currency
actually matches the order. These tests exercise each provider route
end-to-end (signature → handle_webhook → _mark_order_paid) with a FakeDB,
matching the established test_payment_refund_route.py pattern.

Per-provider payload reality (see backend/app/payments/*.py docstrings):
  - PayFast: amount_gross (decimal rand string) + ZAR-only → fully validated.
  - Yoco: amount/currency present on the checkout/payment resource → validated
    when present (best-effort; Yoco payload confidence is documented as
    "Likely", not independently verified against a live account this session).
  - iKhoka / Stitch: the payload this integration was implemented against
    carries no amount at all — a documented, disclosed gap, not silently
    skipped. Their tests below cover business_id scoping, idempotency, and
    failure-after-paid instead of amount forgery (there is nothing to forge
    against).
"""
import json
import urllib.parse
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from backend.app.api.v1.routes_payments import (
    ikhoka_webhook,
    payfast_webhook,
    stitch_webhook,
    yoco_webhook,
)
from backend.app.payments.ikhoka import _sign as ikhoka_sign
from backend.app.payments.payfast import _build_signature as payfast_sign
from backend.app.payments.stitch import StitchProvider


# ── Fakes ─────────────────────────────────────────────────────────────────────

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
        self.added = []
        self.commits = 0
        self.refreshes = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self.refreshes += 1


def make_order(business_id, **overrides):
    class Order:
        pass
    o = Order()
    o.id = uuid.uuid4()
    o.business_id = business_id
    o.order_number = "BO-000001"
    o.payment_status = "UNPAID"
    o.payment_required = True
    o.payment_reference = None
    o.total_cents = 9000
    o.currency = "ZAR"
    o.status = "NEW"
    o.customer_id = None
    o.paid_at = None
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


def make_business(**overrides):
    class Biz:
        pass
    b = Biz()
    b.id = uuid.uuid4()
    b.payment_webhook_secret = None
    b.payment_api_key = None
    b.payment_api_secret = None
    b.whatsapp_phone_number_id = None
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ── PayFast ───────────────────────────────────────────────────────────────────

def _payfast_body(params, passphrase):
    signed = dict(params)
    signed["signature"] = payfast_sign(params, passphrase)
    return urllib.parse.urlencode(signed).encode("utf-8")


async def test_payfast_correct_amount_marks_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    params = {"m_payment_id": str(order.id), "payment_status": "COMPLETE", "amount_gross": "90.00", "pf_payment_id": "pf_1"}
    body = _payfast_body(params, "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        result = await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert result == {"received": True}
    assert order.payment_status == "PAID"


async def test_payfast_forged_lower_amount_not_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    # Attacker (or a stale link) reports a much lower amount than the order total.
    params = {"m_payment_id": str(order.id), "payment_status": "COMPLETE", "amount_gross": "1.00", "pf_payment_id": "pf_2"}
    body = _payfast_body(params, "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert order.payment_status == "UNPAID"  # NOT marked paid


async def test_payfast_wrong_currency_not_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000, currency="USD")  # misconfigured order currency
    params = {"m_payment_id": str(order.id), "payment_status": "COMPLETE", "amount_gross": "90.00", "pf_payment_id": "pf_3"}
    body = _payfast_body(params, "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert order.payment_status == "UNPAID"  # PayFast is ZAR-only; USD order can never match


async def test_payfast_duplicate_delivery_single_transition():
    business = make_business()
    order = make_order(business.id, total_cents=9000, payment_status="PAID")  # already paid
    params = {"m_payment_id": str(order.id), "payment_status": "COMPLETE", "amount_gross": "90.00", "pf_payment_id": "pf_4"}
    body = _payfast_body(params, "secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert order.payment_status == "PAID"
    assert db.commits == 0  # idempotent no-op, no re-write


async def test_payfast_failure_after_paid_still_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000, payment_status="PAID")
    params = {"m_payment_id": str(order.id), "payment_status": "FAILED", "amount_gross": "90.00"}
    body = _payfast_body(params, "secret")
    db = FakeDB([FakeResult(scalar=business)])  # FAILED never reaches _mark_order_paid

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)

    assert order.payment_status == "PAID"  # untouched


async def test_payfast_other_tenants_order_id_not_found():
    business_a = make_business()
    other_business_id = uuid.uuid4()
    order_owned_by_b = make_order(other_business_id, total_cents=9000)
    params = {"m_payment_id": str(order_owned_by_b.id), "payment_status": "COMPLETE", "amount_gross": "90.00"}
    body = _payfast_body(params, "secret")
    # Business A is real and signature is valid for A's passphrase, but the
    # order_id in the payload belongs to business B — the tenant-scoped
    # query (Order.id==X AND Order.business_id==A) must find nothing.
    db = FakeDB([FakeResult(scalar=business_a), FakeResult(scalar=None)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"):
        await payfast_webhook(business_id=business_a.id, request=FakeRequest(body), db=db)

    assert order_owned_by_b.payment_status == "UNPAID"  # never touched
    assert db.commits == 0


async def test_payfast_empty_passphrase_rejects():
    """Empty passphrase must fail closed (PayFastProvider.verify_signature)."""
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    params = {"m_payment_id": str(order.id), "payment_status": "COMPLETE", "amount_gross": "90.00"}
    body = _payfast_body(params, "")  # attacker doesn't need any real secret for this
    db = FakeDB([FakeResult(scalar=business)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value=""):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db)
    assert exc.value.status_code == 400


# ── Yoco ──────────────────────────────────────────────────────────────────────

def _yoco_body(order_id: str, amount: int | None, currency: str | None, status="successful"):
    payload = {
        "type": "checkout.completed",
        "payload": {
            "id": "chr_test123",
            "status": status,
            "metadata": {"orderId": order_id},
        },
    }
    if amount is not None:
        payload["payload"]["amount"] = amount
    if currency is not None:
        payload["payload"]["currency"] = currency
    return json.dumps(payload).encode("utf-8")


async def test_yoco_correct_amount_marks_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    body = _yoco_body(str(order.id), amount=9000, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        result = await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert result == {"received": True}
    assert order.payment_status == "PAID"


async def test_yoco_forged_lower_amount_not_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    body = _yoco_body(str(order.id), amount=100, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert order.payment_status == "UNPAID"


async def test_yoco_wrong_currency_not_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000, currency="ZAR")
    body = _yoco_body(str(order.id), amount=9000, currency="USD")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert order.payment_status == "UNPAID"


async def test_yoco_missing_amount_field_still_marks_paid():
    """
    Best-effort validation: when the payload genuinely doesn't carry an
    amount, the existing signature-only trust model applies (documented,
    not a silent weakening — see module docstring).
    """
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    body = _yoco_body(str(order.id), amount=None, currency=None)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=True):
        await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "sig"}), db=db)

    assert order.payment_status == "PAID"


async def test_yoco_invalid_signature_rejects():
    business = make_business()
    body = _yoco_body(str(uuid.uuid4()), amount=9000, currency="ZAR")
    db = FakeDB([FakeResult(scalar=business)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="whsec"), \
         patch("backend.app.payments.yoco.YocoProvider.verify_signature", return_value=False):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await yoco_webhook(business_id=business.id, request=FakeRequest(body, {"X-Yoco-Signature": "bad"}), db=db)
    assert exc.value.status_code == 400


# ── iKhoka (documented amount gap — scoping/idempotency covered instead) ────

def _ikhoka_body(order_id: str, status="SUCCESS", response_code="00"):
    payload = {
        "paylinkID": "pl_test1",
        "status": status,
        "externalTransactionID": order_id,
        "responseCode": response_code,
    }
    compact = json.dumps(payload, separators=(",", ":"))
    return compact.encode("utf-8"), compact


async def test_ikhoka_valid_webhook_marks_paid_no_amount_available():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    raw, compact = _ikhoka_body(str(order.id))
    callback_path = f"/v1/payments/webhooks/ikhoka/{business.id}"
    sig = ikhoka_sign(callback_path, compact, "app_secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="app_secret"):
        result = await ikhoka_webhook(business_id=business.id, request=FakeRequest(raw, {"ik-sign": sig}), db=db)

    assert result == {"received": True}
    assert order.payment_status == "PAID"  # signature-only trust — documented gap


async def test_ikhoka_other_tenants_order_id_not_found():
    business_a = make_business()
    other_business_id = uuid.uuid4()
    order_owned_by_b = make_order(other_business_id, total_cents=9000)
    raw, compact = _ikhoka_body(str(order_owned_by_b.id))
    callback_path = f"/v1/payments/webhooks/ikhoka/{business_a.id}"
    sig = ikhoka_sign(callback_path, compact, "app_secret")
    db = FakeDB([FakeResult(scalar=business_a), FakeResult(scalar=None)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="app_secret"):
        await ikhoka_webhook(business_id=business_a.id, request=FakeRequest(raw, {"ik-sign": sig}), db=db)

    assert order_owned_by_b.payment_status == "UNPAID"


async def test_ikhoka_duplicate_delivery_single_transition():
    business = make_business()
    order = make_order(business.id, total_cents=9000, payment_status="PAID")
    raw, compact = _ikhoka_body(str(order.id))
    callback_path = f"/v1/payments/webhooks/ikhoka/{business.id}"
    sig = ikhoka_sign(callback_path, compact, "app_secret")
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="app_secret"):
        await ikhoka_webhook(business_id=business.id, request=FakeRequest(raw, {"ik-sign": sig}), db=db)

    assert order.payment_status == "PAID"
    assert db.commits == 0


# ── Stitch (documented amount gap — scoping/idempotency covered instead) ────

def _stitch_body(order_id: str, status_type="Completed", event_type="payment_initiation_request.complete"):
    payload = {
        "type": event_type,
        "data": {"id": "pir_test1", "externalReference": order_id, "status": {"type": status_type}},
    }
    return json.dumps(payload).encode("utf-8")


async def test_stitch_valid_webhook_marks_paid_no_amount_available():
    business = make_business()
    order = make_order(business.id, total_cents=9000)
    body = _stitch_body(str(order.id))
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=order)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="client_secret"), \
         patch("backend.app.payments.stitch.StitchProvider.verify_signature", return_value=True):
        result = await stitch_webhook(business_id=business.id, request=FakeRequest(body, {"X-Stitch-Signature": "sig"}), db=db)

    assert result == {"received": True}
    assert order.payment_status == "PAID"


async def test_stitch_other_tenants_order_id_not_found():
    business_a = make_business()
    other_business_id = uuid.uuid4()
    order_owned_by_b = make_order(other_business_id, total_cents=9000)
    body = _stitch_body(str(order_owned_by_b.id))
    db = FakeDB([FakeResult(scalar=business_a), FakeResult(scalar=None)])

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="client_secret"), \
         patch("backend.app.payments.stitch.StitchProvider.verify_signature", return_value=True):
        await stitch_webhook(business_id=business_a.id, request=FakeRequest(body, {"X-Stitch-Signature": "sig"}), db=db)

    assert order_owned_by_b.payment_status == "UNPAID"


async def test_stitch_failure_after_paid_still_paid():
    business = make_business()
    order = make_order(business.id, total_cents=9000, payment_status="PAID")
    body = _stitch_body(str(order.id), status_type="Failed", event_type="payment_initiation_request.complete")
    db = FakeDB([FakeResult(scalar=business)])  # not-paid result never reaches _mark_order_paid

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="client_secret"), \
         patch("backend.app.payments.stitch.StitchProvider.verify_signature", return_value=True):
        await stitch_webhook(business_id=business.id, request=FakeRequest(body, {"X-Stitch-Signature": "sig"}), db=db)

    assert order.payment_status == "PAID"
