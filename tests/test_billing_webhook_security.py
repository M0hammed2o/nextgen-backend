"""
Fix group 2 (wave0a/sec) — billing webhook signature security (BUG-0001).

Covers:
  - Stripe /v1/billing/webhook: missing secret rejects, malformed signature
    rejects, valid signature (hand-crafted with the real `stripe` library's
    own signing scheme) passes.
  - PayFast /v1/billing/webhooks/payfast: empty passphrase rejects (fails
    closed rather than accepting an unsigned-in-practice request), valid
    MD5 signature (hand-crafted with a test passphrase, via the real
    PayFastProvider._build_signature-equivalent) passes.
  - The inactive provider's billing route rejects (404) regardless of
    signature validity, and both reject when BILLING_ENABLED is false.
  - Duplicate delivery (same provider_reference) does not create a second
    BillingInvoice / re-trigger the lifecycle transition.
"""
import time
import types
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from backend.app.billing.stripe_webhooks import payfast_billing_webhook, stripe_webhook
from backend.app.payments.payfast import _build_signature


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

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

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(),
        billing_status="ACTIVE",
        is_active=True,
        suspended_reason=None,
        billing_period_started_at=datetime.now(timezone.utc) - timedelta(days=1),
        plan="STARTER",
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_123",
        payfast_subscription_token=None,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def _stripe_event_body(event_type: str, data_object: dict) -> tuple[bytes, str]:
    """Build a real Stripe-style event payload string (not yet signed)."""
    import json
    payload = json.dumps({
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": event_type,
        "data": {"object": data_object},
    })
    return payload.encode("utf-8"), payload


def _sign_stripe(payload: str, secret: str) -> str:
    import stripe
    ts = int(time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{ts}.{payload}", secret)
    return f"t={ts},v1={sig}"


# ── Stripe webhook: signature enforcement ────────────────────────────────────

async def test_stripe_webhook_missing_secret_rejects():
    body, payload = _stripe_event_body("invoice.paid", {"id": "in_1", "customer": "cus_1", "amount_paid": 1000})
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"
        mock_settings.STRIPE_WEBHOOK_SECRET = ""

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature="t=1,v1=whatever")
    assert exc.value.status_code == 503
    assert db.commits == 0


async def test_stripe_webhook_malformed_signature_rejects():
    body, payload = _stripe_event_body("invoice.paid", {"id": "in_1", "customer": "cus_1", "amount_paid": 1000})
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"
        mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_a_real_looking_test_secret_value"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature="t=1,v1=not-a-real-signature")
    assert exc.value.status_code == 400
    assert db.commits == 0


async def test_stripe_webhook_valid_signature_passes():
    secret = "whsec_a_real_looking_test_secret_value"
    business = make_business()
    body, payload = _stripe_event_body("invoice.paid", {
        "id": "in_valid_1", "customer": business.stripe_customer_id, "amount_paid": 1000,
    })
    sig_header = _sign_stripe(payload, secret)
    # execute() call order inside _handle_invoice_paid: find business, then
    # duplicate-invoice existence check (None == not a duplicate).
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"
        mock_settings.STRIPE_WEBHOOK_SECRET = secret

        result = await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature=sig_header)

    assert result == {"status": "ok"}
    assert business.billing_status == "ACTIVE"
    assert db.commits == 1


async def test_stripe_webhook_development_skips_signature_check():
    body, payload = _stripe_event_body("customer.subscription.updated", {"id": "sub_x", "customer": "cus_x", "status": "active"})
    # _find_business_by_stripe tries subscription_id first, then customer_id —
    # both miss here (business genuinely not found), so two executes happen.
    db = FakeDB([FakeResult(scalar=None), FakeResult(scalar=None)])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "development"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"

        result = await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature=None)
    assert result == {"status": "ok"}


# ── Stripe webhook: inactive-provider / billing-disabled gating ─────────────

async def test_stripe_webhook_rejects_when_payfast_is_active_provider():
    body, _ = _stripe_event_body("invoice.paid", {"id": "in_1", "customer": "cus_1"})
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "PAYFAST"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature="sig")
    assert exc.value.status_code == 404


async def test_stripe_webhook_rejects_when_billing_disabled():
    body, _ = _stripe_event_body("invoice.paid", {"id": "in_1", "customer": "cus_1"})
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = False
        mock_settings.BILLING_PROVIDER = "STRIPE"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature="sig")
    assert exc.value.status_code == 404


# ── Stripe webhook: duplicate delivery is idempotent ─────────────────────────

async def test_stripe_webhook_duplicate_invoice_paid_does_not_double_write():
    secret = "whsec_a_real_looking_test_secret_value"
    business = make_business()
    body, payload = _stripe_event_body("invoice.paid", {
        "id": "in_dup_1", "customer": business.stripe_customer_id, "amount_paid": 1000,
    })
    sig_header = _sign_stripe(payload, secret)
    # Duplicate: the existence check returns a truthy scalar (already recorded).
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar="already-there")])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"
        mock_settings.STRIPE_WEBHOOK_SECRET = secret

        result = await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature=sig_header)

    assert result == {"status": "ok"}
    assert db.commits == 0  # never reached mark_invoice_paid/db.commit()
    assert db.added == []


# ── Stripe webhook: stale payment_failed does not downgrade a re-paid business ─

async def test_stale_payment_failed_does_not_flip_already_paid_business():
    """
    An invoice.payment_failed event whose `created` timestamp predates the
    business's current billing period (it has paid successfully since) must
    not flip billing_status back to PAST_DUE.
    """
    secret = "whsec_a_real_looking_test_secret_value"
    business = make_business(
        billing_status="ACTIVE",
        billing_period_started_at=datetime.now(timezone.utc),  # just paid
    )
    stale_created = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp())
    body, payload = _stripe_event_body("invoice.payment_failed", {
        "id": "in_stale_1", "customer": business.stripe_customer_id,
        "amount_due": 1000, "created": stale_created,
    })
    sig_header = _sign_stripe(payload, secret)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"
        mock_settings.STRIPE_WEBHOOK_SECRET = secret

        await stripe_webhook(request=FakeRequest(body), db=db, stripe_signature=sig_header)

    assert business.billing_status == "ACTIVE"  # NOT flipped to PAST_DUE
    assert db.added == []  # no FAILED BillingInvoice row recorded for the stale event


# ── PayFast billing ITN ───────────────────────────────────────────────────────

def _payfast_form_body(params: dict, passphrase: str | None) -> bytes:
    signed = dict(params)
    signed["signature"] = _build_signature(params, passphrase)
    return urllib.parse.urlencode(signed).encode("utf-8")


async def test_payfast_billing_webhook_empty_passphrase_rejects():
    business = make_business()
    params = {"m_payment_id": str(business.id), "payment_status": "COMPLETE", "amount_gross": "499.00"}
    body = _payfast_form_body(params, passphrase="")  # attacker needs no real secret to compute this
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "PAYFAST"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = ""

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_billing_webhook(request=FakeRequest(body), db=db)
    assert exc.value.status_code == 400
    assert db.commits == 0


async def test_payfast_billing_webhook_wrong_passphrase_rejects():
    business = make_business()
    params = {"m_payment_id": str(business.id), "payment_status": "COMPLETE", "amount_gross": "499.00"}
    body = _payfast_form_body(params, passphrase="attacker-guess")
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "PAYFAST"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = "the-real-passphrase"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_billing_webhook(request=FakeRequest(body), db=db)
    assert exc.value.status_code == 400


async def test_payfast_billing_webhook_valid_signature_passes():
    business = make_business()
    passphrase = "the-real-passphrase"
    params = {"m_payment_id": str(business.id), "payment_status": "COMPLETE", "amount_gross": "499.00", "pf_payment_id": "pf_ok_1"}
    body = _payfast_form_body(params, passphrase=passphrase)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "PAYFAST"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = passphrase

        result = await payfast_billing_webhook(request=FakeRequest(body), db=db)

    assert result == {"received": True}
    assert business.billing_status == "ACTIVE"
    assert db.commits == 1


async def test_payfast_billing_webhook_rejects_when_stripe_is_active_provider():
    body = _payfast_form_body({"m_payment_id": "x", "payment_status": "COMPLETE"}, passphrase="whatever")
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "STRIPE"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_billing_webhook(request=FakeRequest(body), db=db)
    assert exc.value.status_code == 404


async def test_payfast_billing_webhook_rejects_when_billing_disabled():
    body = _payfast_form_body({"m_payment_id": "x", "payment_status": "COMPLETE"}, passphrase="whatever")
    db = FakeDB([])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = False
        mock_settings.BILLING_PROVIDER = "PAYFAST"

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await payfast_billing_webhook(request=FakeRequest(body), db=db)
    assert exc.value.status_code == 404


async def test_payfast_billing_webhook_duplicate_delivery_does_not_double_write():
    business = make_business()
    passphrase = "the-real-passphrase"
    params = {"m_payment_id": str(business.id), "payment_status": "COMPLETE", "amount_gross": "499.00", "pf_payment_id": "pf_dup_1"}
    body = _payfast_form_body(params, passphrase=passphrase)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar="already-recorded")])

    with patch("backend.app.billing.stripe_webhooks.settings") as mock_settings:
        mock_settings.BILLING_ENABLED = True
        mock_settings.BILLING_PROVIDER = "PAYFAST"
        mock_settings.PAYFAST_BILLING_PASSPHRASE = passphrase

        result = await payfast_billing_webhook(request=FakeRequest(body), db=db)

    assert result == {"received": True}
    assert db.added == []  # no second BillingInvoice added
    assert db.commits == 1  # still commits the subscription-token update path


# ── Decimal amount conversion (no float rounding) ────────────────────────────

def test_rand_amount_to_cents_uses_decimal_not_float():
    from backend.app.payments.payfast import rand_amount_to_cents
    # A value picked specifically because float(...) * 100 rounding is
    # historically unreliable for some decimal rand amounts.
    assert rand_amount_to_cents("90.10") == 9010
    assert rand_amount_to_cents("0.01") == 1
    assert rand_amount_to_cents("1234.56") == 123456
    assert rand_amount_to_cents(None) is None
    assert rand_amount_to_cents("not-a-number") is None
