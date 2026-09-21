"""
Yoco payment provider — Model B (each business uses their own Yoco account).

Setup for business owners:
  1. Sign up at yoco.com → Developer → API Keys → copy Secret Key.
  2. In Yoco dashboard → Webhooks → add URL:
       https://<your-api-domain>/v1/payments/webhooks/yoco/<business_id>
     Copy the Webhook Secret shown after saving.
  3. In NextGen Settings → Payment → paste Secret Key + Webhook Secret.

Credential mapping (stored on Business model):
  payment_api_key        = Yoco Secret Key  (sk_live_...)
  payment_webhook_secret = Yoco Webhook Secret

Apple Pay and Google Pay are automatically available on Yoco's hosted
checkout page — no extra configuration needed.

Yoco API docs: https://developer.yoco.com/online/resources/integration-types
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from backend.app.payments.base import PaymentProvider, ProviderHealthStatus, RefundResult

logger = logging.getLogger("nextgen.payments.yoco")

_CHECKOUT_URL = "https://payments.yoco.com/api/checkouts"
_PAYMENTS_URL = "https://payments.yoco.com/api/payments"


class YocoProvider(PaymentProvider):

    async def create_payment_link(self, order, business) -> str | None:
        """Call Yoco Checkouts API and return the hosted payment URL."""
        from backend.app.core.crypto import decrypt_credential
        api_key = decrypt_credential(getattr(business, "payment_api_key", None))
        if not api_key:
            logger.warning("Yoco: no api_key configured for business %s", getattr(business, "id", "?"))
            return None

        payload = {
            "amount": order.total_cents,
            "currency": getattr(business, "currency", "ZAR"),
            "metadata": {
                "orderId": str(order.id),
                "orderNumber": order.order_number,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _CHECKOUT_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                # Yoco returns "url" or "redirectUrl" depending on API version
                url = data.get("url") or data.get("redirectUrl")
                logger.info("Yoco checkout created: order=%s", order.order_number)
                return url
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Yoco API %s error for order %s: %s",
                exc.response.status_code, order.order_number, exc.response.text,
            )
            return None
        except Exception:
            logger.exception("Yoco create_payment_link failed for order %s", order.order_number)
            return None

    async def verify_payment(self, order, business) -> bool:
        # Confirmation is handled exclusively via webhook — no polling needed.
        return False

    async def handle_webhook(self, payload: dict) -> dict:
        """
        Parse a Yoco webhook event.

        Successful event types:
          - checkout.completed  (checkout reached terminal successful state)
          - payment.succeeded   (individual payment within a checkout succeeded)

        Payload structure:
          {
            "id": "evt_xxx",
            "type": "checkout.completed",
            "payload": {
              "id": "chr_xxx",
              "status": "successful",
              "metadata": { "orderId": "<uuid>", "orderNumber": "BO-000123" }
            }
          }
        """
        event_type = payload.get("type", "")
        inner = payload.get("payload", {})
        status = inner.get("status", "")
        metadata = inner.get("metadata", {})
        order_id = metadata.get("orderId")

        paid = event_type in ("checkout.completed", "payment.succeeded") and status == "successful"
        # "id" here is Yoco's own charge/payment id (e.g. "chr_xxx") — captured
        # so a later refund can reference the actual provider transaction,
        # not just our own order UUID.
        reference = inner.get("id")
        # Yoco's checkout/payment resource carries amount (integer cents,
        # matching what create_payment_link sends) and currency directly on
        # the same object this webhook reports — used by the caller to
        # verify the paid amount actually matches the order before marking
        # it PAID. Best-effort: not present on every Yoco event type, so a
        # missing value here means "could not verify," not "verified zero."
        amount_cents = inner.get("amount")
        currency = inner.get("currency")
        return {
            "paid": paid,
            "order_id": order_id,
            "reference": reference,
            "amount_cents": amount_cents if isinstance(amount_cents, int) else None,
            "currency": currency,
        }

    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        """
        Refund a Yoco payment by its charge/payment id.

        UNVERIFIED against live credentials or a sandbox account — this
        session had no Yoco API access to confirm the exact refund endpoint
        path/response shape. Implemented against Yoco's documented REST
        conventions (same Bearer-token auth as create_payment_link, refund
        endpoint under /api/payments/{id}/refund). Confirm against Yoco's
        current API reference (https://developer.yoco.com) before enabling
        real refunds in production — until then this fails safely (caught
        exception → RefundResult(success=False)), it will not silently
        corrupt `Order.payment_status`.
        """
        from backend.app.core.crypto import decrypt_credential
        api_key = decrypt_credential(getattr(business, "payment_api_key", None))
        if not api_key:
            return RefundResult(success=False, status="FAILED", error="No Yoco api_key configured")

        reference = getattr(order, "payment_reference", None)
        if not reference:
            return RefundResult(
                success=False, status="FAILED",
                error="No Yoco charge reference stored on this order — cannot target a refund",
            )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_PAYMENTS_URL}/{reference}/refund",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"amount": amount_cents},
                )
                resp.raise_for_status()
                data = resp.json()
                return RefundResult(success=True, status="COMPLETED", provider_reference=data.get("id", reference))
        except httpx.HTTPStatusError as exc:
            logger.error("Yoco refund %s error for order %s: %s", exc.response.status_code, order.order_number, exc.response.text)
            return RefundResult(success=False, status="FAILED", error=f"Yoco API error: {exc.response.status_code}")
        except Exception as exc:
            logger.exception("Yoco refund_payment failed for order %s", order.order_number)
            return RefundResult(success=False, status="FAILED", error=str(exc))

    async def check_health(self, business) -> ProviderHealthStatus:
        from backend.app.core.crypto import decrypt_credential
        api_key = decrypt_credential(getattr(business, "payment_api_key", None))
        if not api_key:
            return ProviderHealthStatus.now(healthy=False, provider="YOCO", detail="No api_key configured")
        if not api_key.startswith(("sk_live_", "sk_test_")):
            return ProviderHealthStatus.now(healthy=False, provider="YOCO", detail="api_key doesn't look like a Yoco secret key")
        return ProviderHealthStatus.now(healthy=True, provider="YOCO", detail="Credentials configured (live connectivity not checked)")

    @staticmethod
    def verify_signature(raw_body: bytes, signature_header: str, webhook_secret: str) -> bool:
        """
        Verify the X-Yoco-Signature header.

        Yoco signs webhook payloads as: "sha256=<HMAC-SHA256-hex>"
        where the key is the webhook secret from your Yoco dashboard.
        """
        if not signature_header or not webhook_secret:
            return False
        expected = "sha256=" + hmac.new(
            webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)
