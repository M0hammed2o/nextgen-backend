"""
Uber Eats environment resolution — production vs sandbox.

Uber issues a SEPARATE application (its own client id/secret) for sandbox
work, on separate hosts. Every outbound call and every token is therefore
chosen from the CONNECTION's environment (`DeliveryConnection.environment`),
never from a process-wide switch: one deployment can hold both apps, and a
sandbox order can never be fetched/accepted with production credentials (or
the reverse). The pre-existing `UBER_EATS_ENVIRONMENT` setting was read by
nothing at all (BUG-0094) — this module is what replaces it.

Inbound webhooks carry no trustworthy environment: `X-Environment` is an
unauthenticated header. `match_webhook_environment()` instead reports which
configured app secret actually verifies the signature — an environment that
is proven, not claimed.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from backend.app.core.config import get_settings
from backend.app.delivery.errors import DeliveryProviderError

PRODUCTION = "production"
SANDBOX = "sandbox"
ENVIRONMENTS = (PRODUCTION, SANDBOX)


@dataclass(frozen=True)
class UberEnvironmentConfig:
    name: str
    client_id: str
    client_secret: str
    api_base_url: str
    auth_base_url: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)


def get_environment_config(environment: str | None) -> UberEnvironmentConfig:
    """Credentials + hosts for one environment. Unknown values are refused, never defaulted."""
    settings = get_settings()
    env = (environment or "").strip().lower()
    if env == PRODUCTION:
        return UberEnvironmentConfig(
            name=PRODUCTION,
            client_id=settings.UBER_EATS_CLIENT_ID,
            client_secret=settings.UBER_EATS_CLIENT_SECRET,
            api_base_url=settings.UBER_EATS_API_BASE_URL.rstrip("/"),
            auth_base_url=settings.UBER_EATS_AUTH_BASE_URL.rstrip("/"),
        )
    if env == SANDBOX:
        return UberEnvironmentConfig(
            name=SANDBOX,
            client_id=settings.UBER_EATS_SANDBOX_CLIENT_ID,
            client_secret=settings.UBER_EATS_SANDBOX_CLIENT_SECRET,
            api_base_url=settings.UBER_EATS_SANDBOX_API_BASE_URL.rstrip("/"),
            auth_base_url=settings.UBER_EATS_SANDBOX_AUTH_BASE_URL.rstrip("/"),
        )
    raise DeliveryProviderError("UBER_EATS", f"unknown environment {environment!r}")


def match_webhook_environment(raw_body: bytes, signature_header: str | None) -> str | None:
    """
    Return the environment whose app secret verifies `X-Uber-Signature`
    (lowercase hex HMAC-SHA256 of the raw body, keyed by the client secret —
    developer.uber.com/docs/eats/guides/webhooks), or None if no configured
    secret does. Production is tried first. Constant-time comparison.
    """
    if not signature_header:
        return None
    presented = signature_header.strip().lower()
    for env in ENVIRONMENTS:
        secret = get_environment_config(env).client_secret
        if not secret:
            continue
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, presented):
            return env
    return None
