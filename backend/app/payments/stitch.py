"""
Stitch payment provider — Model B (each business uses their own Stitch account).

Stitch provides instant bank-to-bank payments (Pay by Bank / EFT) with no card
required, making it ideal for South African customers who prefer not to share
card details online. Samsung Pay, Google Pay support is via their card partners.

Setup for business owners:
  1. Apply for a Stitch account at stitch.money (requires approval).
  2. Once approved, go to Dashboard → Clients → create a Client.
     Note the Client ID and Client Secret.
  3. In Stitch Dashboard → Webhooks → add:
       https://<your-api-domain>/v1/payments/webhooks/stitch/<business_id>
  4. Link your bank account in Stitch Dashboard → Accounts.
  5. In NextGen Settings → Payment, enter:
       payment_api_key    = Stitch Client Secret
       payment_api_secret = Stitch Client ID

IMPORTANT — bank account for receiving funds:
  Stitch pays into the bank account you link in their dashboard.
  The NextGen system uses the business's EFT bank details (eft_bank_name,
  eft_account_name, eft_account_number) to describe the beneficiary in the
  payment request. These must match the account you linked in Stitch.

Stitch API docs: https://stitch.money/docs
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

import httpx

from backend.app.payments.base import PaymentProvider

logger = logging.getLogger("nextgen.payments.stitch")

_TOKEN_URL = "https://secure.stitch.money/connect/token"
_GRAPHQL_URL = "https://api.stitch.money/graphql"

# GraphQL mutation to create a payment initiation request
_CREATE_PAYMENT_MUTATION = """
mutation CreatePaymentRequest($input: ClientPaymentInitiationRequestInput!) {
  clientPaymentInitiationRequestCreate(input: $input) {
    paymentInitiationRequest {
      id
      url
    }
  }
}
"""

# Simple in-process token cache to avoid fetching a new token on every request.
# Key: (client_id, client_secret) → (access_token, expires_at)
_token_cache: dict[tuple[str, str], tuple[str, datetime]] = {}


async def _get_access_token(client_id: str, client_secret: str) -> str | None:
    """Fetch or return cached OAuth2 client-credentials token from Stitch."""
    cache_key = (client_id, client_secret)
    cached = _token_cache.get(cache_key)
    if cached:
        token, expires_at = cached
        # Refresh 60s before expiry
        if datetime.now(timezone.utc).timestamp() < expires_at.timestamp() - 60:
            return token

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "audience": "https://secure.stitch.money/connect/token",
                    "scope": "client_paymentrequest",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()
            access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
            expires_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + expires_in,
                tz=timezone.utc,
            )
            _token_cache[cache_key] = (access_token, expires_at)
            return access_token
    except Exception:
        logger.exception("Stitch token fetch failed")
        return None


# Map common SA bank names to Stitch bank IDs
_BANK_NAME_TO_STITCH_ID: dict[str, str] = {
    "fnb": "fnb",
    "first national bank": "fnb",
    "nedbank": "nedbank",
    "standard bank": "standard_bank",
    "standardbank": "standard_bank",
    "absa": "absa",
    "capitec": "capitec",
    "capitec bank": "capitec",
    "investec": "investec",
    "tyme bank": "tymebank",
    "tymebank": "tymebank",
    "african bank": "african_bank",
    "access bank": "access_bank",
    "grindrod bank": "grindrod",
}


def _resolve_bank_id(bank_name: str | None) -> str:
    if not bank_name:
        return "fnb"
    return _BANK_NAME_TO_STITCH_ID.get(bank_name.strip().lower(), bank_name.lower().replace(" ", "_"))


class StitchProvider(PaymentProvider):

    async def create_payment_link(self, order, business) -> str | None:
        """
        Create a Stitch payment initiation request and return the payment URL.

        The URL is sent to the customer via WhatsApp. They open it in their
        browser, select their bank, and authorise the payment — funds land
        directly in the business's linked bank account.
        """
        from backend.app.core.crypto import decrypt_credential
        client_secret = decrypt_credential(getattr(business, "payment_api_key", None))
        client_id = decrypt_credential(getattr(business, "payment_api_secret", None))
        if not client_secret or not client_id:
            logger.warning(
                "Stitch: missing client_id or client_secret for business %s",
                getattr(business, "id", "?"),
            )
            return None

        access_token = await _get_access_token(client_id, client_secret)
        if not access_token:
            return None

        from backend.app.core.config import get_settings
        cfg = get_settings()

        # Amount in Stitch is a decimal string
        amount_str = f"{order.total_cents / 100:.2f}"
        currency = getattr(business, "currency", "ZAR")

        # Bank account of the business (must match what's linked in Stitch dashboard)
        bank_name = getattr(business, "eft_bank_name", None)
        account_name = getattr(business, "eft_account_name", None) or business.name
        account_number = getattr(business, "eft_account_number", None)

        if not account_number:
            logger.warning(
                "Stitch: no eft_account_number on business %s — cannot create payment request",
                getattr(business, "id", "?"),
            )
            return None

        variables = {
            "input": {
                "amount": {"quantity": amount_str, "currency": currency},
                "payerReference": f"Order {order.order_number}",
                "beneficiaryReference": order.order_number,
                "externalReference": str(order.id),
                "redirectUri": cfg.PAYMENT_RETURN_URL,
                "beneficiary": {
                    "bankAccount": {
                        "name": account_name,
                        "bankId": _resolve_bank_id(bank_name),
                        "accountNumber": account_number,
                        "accountType": "current",
                    }
                },
            }
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    _GRAPHQL_URL,
                    json={"query": _CREATE_PAYMENT_MUTATION, "variables": variables},
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                errors = data.get("errors")
                if errors:
                    logger.error("Stitch GraphQL errors for order %s: %s", order.order_number, errors)
                    return None

                request_data = (
                    data
                    .get("data", {})
                    .get("clientPaymentInitiationRequestCreate", {})
                    .get("paymentInitiationRequest", {})
                )
                url = request_data.get("url")
                logger.info("Stitch payment request created for order %s", order.order_number)
                return url
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Stitch API %s error for order %s: %s",
                exc.response.status_code, order.order_number, exc.response.text,
            )
            return None
        except Exception:
            logger.exception("Stitch create_payment_link failed for order %s", order.order_number)
            return None

    async def verify_payment(self, order, business) -> bool:
        # Handled via webhook — no polling needed.
        return False

    async def handle_webhook(self, payload: dict) -> dict:
        """
        Parse a Stitch webhook event.

        Stitch sends events like:
          {
            "type": "payment_initiation_request.complete",
            "data": {
              "id": "pir_xxx",
              "externalReference": "<order_uuid>",
              "status": { "type": "Completed" }
            }
          }
        """
        event_type = payload.get("type", "")
        data = payload.get("data", {})
        order_id = data.get("externalReference")
        status = data.get("status", {})
        status_type = status.get("type", "") if isinstance(status, dict) else str(status)

        paid = (
            event_type == "payment_initiation_request.complete"
            and status_type == "Completed"
        )
        return {"paid": paid, "order_id": order_id}

    @staticmethod
    def verify_signature(raw_body: bytes, signature_header: str, client_secret: str) -> bool:
        """
        Verify the X-Stitch-Signature header.

        Stitch signs with HMAC-SHA256 using the client secret as the key.
        Header format: "sha256=<hex_digest>"
        """
        if not signature_header or not client_secret:
            return False
        expected = "sha256=" + hmac.new(
            client_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)
