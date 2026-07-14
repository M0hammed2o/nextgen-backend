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

from backend.app.payments.base import PaymentProvider

logger = logging.getLogger("nextgen.payments.payfast")

_LIVE_URL = "https://www.payfast.co.za/eng/process"
_SANDBOX_URL = "https://sandbox.payfast.co.za/eng/process"


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

    async def create_payment_link(self, order, business) -> str | None:
        """
        Build a signed PayFast payment URL.

        PayFast works via a redirect — we construct a URL with signed params
        and send it to the customer. They click it, pay on PayFast's hosted
        page, and PayFast POSTs the result to our notify_url.
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

        params: dict[str, str] = {
            "merchant_id": str(merchant_id),
            "merchant_key": str(merchant_key),
            "return_url": cfg.PAYMENT_RETURN_URL,
            "cancel_url": cfg.PAYMENT_CANCEL_URL,
            "notify_url": f"{cfg.BACKEND_PUBLIC_URL}/v1/payments/webhooks/payfast/{business.id}",
            "amount": f"{order.total_cents / 100:.2f}",
            "item_name": f"Order {order.order_number}",
            "m_payment_id": str(order.id),
            "email_confirmation": "1",
        }

        params["signature"] = _build_signature(params, passphrase)

        url = f"{_LIVE_URL}?{urllib.parse.urlencode(params)}"
        logger.info("PayFast link built for order %s", order.order_number)
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
          signature     — for verification (already verified in the route)
        """
        order_id = payload.get("m_payment_id", "")
        payment_status = payload.get("payment_status", "")
        paid = payment_status == "COMPLETE"
        return {"paid": paid, "order_id": order_id}

    @staticmethod
    def verify_signature(params: dict, passphrase: str | None) -> bool:
        """
        Verify a PayFast ITN payload by recomputing the MD5 signature.

        Args:
            params: the full form data dict from the ITN POST
            passphrase: the business's PayFast passphrase (stored as payment_webhook_secret)
        """
        received_sig = params.pop("signature", None)
        if not received_sig:
            return False
        expected = _build_signature(params, passphrase)
        return hmac.compare_digest(expected, received_sig)


import hmac  # noqa: E402 — needed by verify_signature, imported at module level for clarity
