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

from backend.app.payments.base import PaymentProvider

logger = logging.getLogger("nextgen.payments.yoco")

_CHECKOUT_URL = "https://payments.yoco.com/api/checkouts"


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
        return {"paid": paid, "order_id": order_id}

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
