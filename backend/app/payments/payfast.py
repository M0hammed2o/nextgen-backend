"""
PayFast payment provider — Model B (each business uses their own PayFast account).

Setup for business owners:
  1. Sign up at payfast.io → Settings → Integration → note Merchant ID + Merchant Key.
  2. Set a Passphrase under Settings → Security.
  3. In NextGen Settings → Payment, enter:
       payment_api_key        = Merchant Key
       payment_api_secret     = Merchant ID
       payment_webhook_secret = Passphrase
  4. PayFast will POST payment notifications to:
       https://<your-api-domain>/v1/payments/webhooks/payfast/<business_id>
     This URL is embedded in every payment link automatically.

PayFast supports: Visa/Mastercard, Apple Pay, Google Pay, Instant EFT,
                  SnapScan, Mobicred, MoreTyme, Zapper.

PayFast docs: https://developers.payfast.co.za
"""

from __future__ import annotations

import hashlib
import logging
import urllib.parse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx

from backend.app.payments.base import (
    PaymentProvider,
    ProviderHealthStatus,
    RefundResult,
    resolve_amount_and_reference,
)

logger = logging.getLogger("nextgen.payments.payfast")

_LIVE_URL = "https://www.payfast.co.za/eng/process"
_SANDBOX_URL = "https://sandbox.payfast.co.za/eng/process"
_REFUND_API_URL = "https://api.payfast.co.za/orders/{payment_id}/refund"


def rand_amount_to_cents(raw: str | None) -> int | None:
    """
    Convert a PayFast decimal-rand amount string (e.g. "amount_gross") to
    integer cents using Decimal arithmetic — never float, which can misround
    real money values (e.g. float(90.10) * 100 != 9010 on some inputs).
    Returns None if `raw` is missing or not a valid decimal.
    """
    if not raw:
        return None
    try:
        return int((Decimal(raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def _build_signature(params: dict, passphrase: str | None) -> str:
    """
    Build the PayFast MD5 signature.

    Sort params alphabetically, URL-encode values, concatenate as key=value&...
    If a passphrase is set, append &passphrase=<encoded_passphrase> before hashing.
    """
    pairs = "&".join(
        f"{k}={urllib.parse.quote_plus(str(v)).replace('+', '+')}"
        for k, v in sorted(params.items())
    )
    if passphrase:
        pairs += f"&passphrase={urllib.parse.quote_plus(passphrase)}"
    return hashlib.md5(pairs.encode("utf-8")).hexdigest()


class PayFastProvider(PaymentProvider):

    async def create_payment_link(
        self,
        order,
        business,
        *,
        amount_cents: int | None = None,
        external_reference: str | None = None,
    ) -> str | None:
        """
        Build a signed PayFast payment URL.

        PayFast works via a redirect — we construct a URL with signed params
        and send it to the customer. They click it, pay on PayFast's hosted
        page, and PayFast POSTs the result to our notify_url.

        order=None is the POS Pay-by-Link intent path — amount_cents and
        external_reference are then mandatory (see resolve_amount_and_reference)
        and are used in place of order.total_cents/str(order.id) for the
        amount and m_payment_id fields; item_name degrades to a generic
        "POS payment" description since there is no order to describe.
        """
        from backend.app.core.crypto import decrypt_credential
        merchant_key = decrypt_credential(getattr(business, "payment_api_key", None))
        merchant_id = decrypt_credential(getattr(business, "payment_api_secret", None))
        if not merchant_key or not merchant_id:
            logger.warning(
                "PayFast: missing merchant_id or merchant_key for business %s",
                getattr(business, "id", "?"),
            )
            return None

        passphrase = decrypt_credential(getattr(business, "payment_webhook_secret", None))

        from backend.app.core.config import get_settings
        cfg = get_settings()

        resolved_amount, resolved_reference = resolve_amount_and_reference(
            order, amount_cents, external_reference
        )
        item_name = f"Order {order.order_number}" if order is not None else "POS payment"
        label = order.order_number if order is not None else resolved_reference

        params: dict[str, str] = {
            "merchant_id": str(merchant_id),
            "merchant_key": str(merchant_key),
            "return_url": cfg.PAYMENT_RETURN_URL,
            "cancel_url": cfg.PAYMENT_CANCEL_URL,
            "notify_url": f"{cfg.BACKEND_PUBLIC_URL}/v1/payments/webhooks/payfast/{business.id}",
            "amount": f"{resolved_amount / 100:.2f}",
            "item_name": item_name,
            "m_payment_id": resolved_reference,
            "email_confirmation": "1",
        }

        params["signature"] = _build_signature(params, passphrase)

        url = f"{_LIVE_URL}?{urllib.parse.urlencode(params)}"
        logger.info("PayFast link built for order %s", label)
        return url

    async def verify_payment(self, order, business) -> bool:
        # Handled via ITN webhook — no polling needed.
        return False

    async def handle_webhook(self, payload: dict) -> dict:
        """
        Parse a PayFast ITN (Instant Transaction Notification) POST.

        PayFast sends application/x-www-form-urlencoded with:
          m_payment_id  — our order UUID
          payment_status — "COMPLETE" | "FAILED" | "CANCELLED"
          amount_gross  — decimal rand string, e.g. "90.00"
          signature     — for verification (already verified in the route)

        amount_cents/currency are returned so the caller (routes_payments.py)
        can verify the paid amount actually matches the order before marking
        it PAID — PayFast is ZAR-only (it has no multi-currency support and
        sends no currency field), so currency is hardcoded here, not parsed.
        """
        order_id = payload.get("m_payment_id", "")
        payment_status = payload.get("payment_status", "")
        paid = payment_status == "COMPLETE"
        # pf_payment_id is PayFast's own transaction id — captured so a later
        # refund can target the real provider transaction.
        reference = payload.get("pf_payment_id")
        amount_cents = rand_amount_to_cents(payload.get("amount_gross"))
        return {
            "paid": paid,
            "order_id": order_id,
            "reference": reference,
            "amount_cents": amount_cents,
            "currency": "ZAR",
        }

    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        """
        Refund a PayFast payment by its pf_payment_id.

        UNVERIFIED against live credentials or a sandbox account. PayFast's
        refund API (api.payfast.co.za) uses a different auth scheme (an API
        key/passphrase-derived token, not the ITN form-signature used
        elsewhere in this file) that this session had no live account to
        confirm against — implemented as a best-effort call using the
        merchant_id/passphrase already configured, but PayFast refunds
        typically also require the merchant's refund permission to be
        enabled by PayFast support first. Confirm both the auth scheme and
        that refunds are enabled on the account before relying on this in
        production; it fails safely (RefundResult(success=False)) on any
        error rather than falsely marking an order refunded.
        """
        from backend.app.core.crypto import decrypt_credential
        merchant_id = decrypt_credential(getattr(business, "payment_api_secret", None))
        passphrase = decrypt_credential(getattr(business, "payment_webhook_secret", None))
        if not merchant_id:
            return RefundResult(success=False, status="FAILED", error="PayFast merchant_id not configured")

        reference = getattr(order, "payment_reference", None)
        if not reference:
            return RefundResult(
                success=False, status="FAILED",
                error="No PayFast pf_payment_id stored on this order — cannot target a refund",
            )

        params = {"merchant-id": merchant_id, "amount": amount_cents}
        signature = _build_signature(params, passphrase)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _REFUND_API_URL.format(payment_id=reference),
                    json=params,
                    headers={"merchant-id": merchant_id, "signature": signature, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                return RefundResult(success=True, status="COMPLETED", provider_reference=data.get("id", reference))
        except httpx.HTTPStatusError as exc:
            logger.error("PayFast refund %s error for order %s: %s", exc.response.status_code, order.order_number, exc.response.text)
            return RefundResult(success=False, status="FAILED", error=f"PayFast API error: {exc.response.status_code}")
        except Exception as exc:
            logger.exception("PayFast refund_payment failed for order %s", order.order_number)
            return RefundResult(success=False, status="FAILED", error=str(exc))

    async def check_health(self, business) -> ProviderHealthStatus:
        from backend.app.core.crypto import decrypt_credential
        merchant_key = decrypt_credential(getattr(business, "payment_api_key", None))
        merchant_id = decrypt_credential(getattr(business, "payment_api_secret", None))
        if not merchant_key or not merchant_id:
            return ProviderHealthStatus.now(healthy=False, provider="PAYFAST", detail="Merchant ID/Key not configured")
        return ProviderHealthStatus.now(healthy=True, provider="PAYFAST", detail="Credentials configured (live connectivity not checked)")

    @staticmethod
    def verify_signature(params: dict, passphrase: str | None) -> bool:
        """
        Verify a PayFast ITN payload by recomputing the MD5 signature.

        Args:
            params: the full form data dict from the ITN POST
            passphrase: the business's PayFast passphrase (stored as payment_webhook_secret)

        An empty/unset passphrase MUST fail closed. _build_signature() simply
        omits the passphrase segment when one isn't supplied, which means the
        "signature" becomes a hash of only the (attacker-controlled, entirely
        public) POST body — anyone can compute a valid-looking signature
        without ever knowing a secret. This covers both the tenant PayFast
        webhook (routes_payments.py::payfast_webhook) and NextGen's own
        PayFast billing ITN (billing/stripe_webhooks.py::payfast_billing_webhook).
        """
        if not passphrase:
            return False
        received_sig = params.pop("signature", None)
        if not received_sig:
            return False
        expected = _build_signature(params, passphrase)
        return hmac.compare_digest(expected, received_sig)


import hmac  # noqa: E402 — needed by verify_signature, imported at module level for clarity
