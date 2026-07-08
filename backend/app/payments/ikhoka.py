"""
iKhoka payment provider — Model B (each business uses their own iKhoka account).

Setup for business owners:
  1. Sign up at ikhokha.com → log in to iK Dashboard.
  2. Navigate to Integrations > Payment API → generate API keys.
  3. In NextGen Settings → Payment → paste Application ID + Application Secret.
  Note: the webhook callback URL is automatically included in each payment link —
  no separate dashboard configuration needed.

Credential mapping (stored on Business model):
  payment_api_key    = Application ID    (IK-APPID header and entityID in body)
  payment_api_secret = Application Secret (HMAC-SHA256 signing key for requests + webhooks)

Signing algorithm (per iKhoka docs):
  IK-SIGN = HMAC-SHA256(path + escaped_body, app_secret)
  escaped_body = compact_json_no_spaces with all " replaced by \"

Supports: Card payments, Google Pay, Apple Pay, Instant EFT
Rate: 2.85% per transaction (decreases with volume)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import httpx

from backend.app.payments.base import PaymentProvider

logger = logging.getLogger("nextgen.payments.ikhoka")

_API_URL = "https://api.ikhokha.com/public-api/v1/api/payment"
_API_PATH = "/public-api/v1/api/payment"


def _sign(path: str, compact_body: str, app_secret: str) -> str:
    """
    iKhoka HMAC-SHA256 request/webhook signature.

    IK-SIGN = HMAC-SHA256(path + escaped_body, app_secret)
    where escaped_body is compact JSON (no spaces) with every " replaced by \"
    """
    escaped = compact_body.replace('"', '\\"')
    payload = (path + escaped).encode("utf-8")
    return hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class IKhokaProvider(PaymentProvider):

    async def create_payment_link(self, order, business) -> str | None:
        """POST to iKhoka payment API and return the hosted paylinkUrl."""
        app_id = getattr(business, "payment_api_key", None)
        app_secret = getattr(business, "payment_api_secret", None)
        if not app_id or not app_secret:
            logger.warning(
                "iKhoka: missing credentials for business %s", getattr(business, "id", "?")
            )
            return None

        from backend.app.core.config import get_settings
        settings = get_settings()

        callback_url = (
            f"{settings.BACKEND_PUBLIC_URL}/v1/payments/webhooks/ikhoka/{business.id}"
        )

        body: dict = {
            "entityID": app_id,
            "externalEntityID": str(business.id),
            "amount": order.total_cents,
            "currency": getattr(business, "currency", "ZAR"),
            "requesterUrl": settings.BACKEND_PUBLIC_URL,
            "description": f"Order {order.order_number}",
            "mode": "live",
            "externalTransactionID": str(order.id),
            "urls": {
                "callbackUrl": callback_url,
                "successPageUrl": settings.PAYMENT_RETURN_URL,
                "failurePageUrl": settings.PAYMENT_CANCEL_URL,
                "cancelUrl": settings.PAYMENT_CANCEL_URL,
            },
        }

        # Compact JSON — no spaces after separators; must match what _sign expects
        body_str = json.dumps(body, separators=(",", ":"))
        signature = _sign(_API_PATH, body_str, app_secret)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _API_URL,
                    content=body_str.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "IK-APPID": app_id,
                        "IK-SIGN": signature,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                link = data.get("paylinkUrl")
                logger.info(
                    "iKhoka paylink created: order=%s paylink=%s",
                    order.order_number,
                    data.get("paylinkID"),
                )
                return link
        except httpx.HTTPStatusError as exc:
            logger.error(
                "iKhoka API %s error for order %s: %s",
                exc.response.status_code,
                order.order_number,
                exc.response.text,
            )
            return None
        except Exception:
            logger.exception("iKhoka create_payment_link failed for order %s", order.order_number)
            return None

    async def verify_payment(self, order, business) -> bool:
        # Confirmation handled exclusively via webhook callback.
        return False

    async def handle_webhook(self, payload: dict) -> dict:
        """
        Parse an iKhoka webhook callback.

        iKhoka sends one callback per payment link after success or failure:
          {
            "paylinkID": "2zh1zj6y8xpb0g3",
            "status": "SUCCESS",
            "externalTransactionID": "<order_uuid>",
            "responseCode": "00"
          }

        status == "SUCCESS" and responseCode == "00" means payment completed.
        The externalTransactionID is what we set to str(order.id) at link creation.
        """
        status = payload.get("status", "")
        response_code = payload.get("responseCode", "")
        order_id = payload.get("externalTransactionID")
        paid = status == "SUCCESS" and response_code == "00"
        return {"paid": paid, "order_id": order_id}

    @staticmethod
    def verify_signature(
        raw_body: bytes,
        ik_sign: str,
        app_secret: str,
        callback_path: str,
    ) -> bool:
        """
        Verify the ik-sign webhook header.

        iKhoka signs the callback with the same algorithm used for API requests:
          HMAC-SHA256(callback_path + escaped_body, app_secret)

        callback_path is the path portion of the callbackUrl set at link creation
        (e.g. '/v1/payments/webhooks/ikhoka/<business_id>').

        The body is re-parsed and re-serialized as compact JSON to normalise
        whitespace before applying the escaping, matching what iKhoka signs.
        """
        if not ik_sign or not app_secret:
            return False
        try:
            parsed = json.loads(raw_body)
            compact = json.dumps(parsed, separators=(",", ":"))
        except Exception:
            return False
        expected = _sign(callback_path, compact, app_secret)
        return hmac.compare_digest(expected, ik_sign)
