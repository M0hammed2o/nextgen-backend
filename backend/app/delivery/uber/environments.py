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
configured key actually verifies the signature — an environment that is
proven, not claimed.

Which key Uber signs with is documented inconsistently. The Eats webhook
guide says the app's client secret. The dashboard's "Basic HMAC" webhook
setting issues dedicated signing keys, and Uber's webhook-setup guide
(developer.uber.com/docs/third-party-rides-demand-v1/guides/webhook-setup,
read 2026-11-06) says `X-Uber-Signature` is keyed by the signing key and
`X-Uber-Signature-New` by the optional secondary key during a rotation, after
which the new key moves into `X-Uber-Signature`. So every configured key of
an environment is tried against both headers, and the label of the key that
matched (never its value) is logged: the first real Uber event shows which
one Uber actually uses.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

from backend.app.core.config import get_settings
from backend.app.delivery.errors import DeliveryProviderError

logger = logging.getLogger(__name__)

PRODUCTION = "production"
SANDBOX = "sandbox"
ENVIRONMENTS = (PRODUCTION, SANDBOX)

#: Headers that may carry a signature, primary first.
SIGNATURE_HEADERS = ("x-uber-signature", "x-uber-signature-new")
#: A well-formed signature: a hex SHA-256 digest. Anything else is refused
#: before comparison (compare_digest raises on non-ASCII text).
_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class UberEnvironmentConfig:
    name: str
    client_id: str
    client_secret: str
    api_base_url: str
    auth_base_url: str
    #: Dashboard "Basic HMAC" signing keys as (label, value); empty values
    #: are dropped. Sandbox only for now — production has none configured.
    webhook_signing_keys: tuple[tuple[str, str], ...] = ()

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
            webhook_signing_keys=tuple(
                (label, value)
                for label, value in (
                    ("signing_key", settings.UBER_EATS_SANDBOX_WEBHOOK_SIGNING_KEY),
                    ("secondary_signing_key", settings.UBER_EATS_SANDBOX_WEBHOOK_SECONDARY_SIGNING_KEY),
                )
                if value
            ),
        )
    raise DeliveryProviderError("UBER_EATS", f"unknown environment {environment!r}")


def _well_formed(signature: str | None) -> str | None:
    if not signature:
        return None
    value = signature.strip().lower()
    return value if _SIGNATURE_RE.fullmatch(value) else None


def match_webhook_environment(
    raw_body: bytes, signature_header: str | None, new_signature_header: str | None = None
) -> str | None:
    """
    Return the environment one of whose keys verifies a presented signature
    (lowercase hex HMAC-SHA256 of the exact raw body), or None if none does.

    Candidates per environment: its dashboard signing keys, then its client
    secret (the key the Eats guide documents). Each is tried against
    `X-Uber-Signature` and `X-Uber-Signature-New`. Production is tried
    first. Malformed or missing headers never match. Constant-time comparison.
    """
    presented = [
        (name, value)
        for name, raw in zip(SIGNATURE_HEADERS, (signature_header, new_signature_header))
        if (value := _well_formed(raw))
    ]
    if not presented:
        return None
    for env in ENVIRONMENTS:
        config = get_environment_config(env)
        candidates = list(config.webhook_signing_keys)
        if config.client_secret:
            candidates.append(("client_secret", config.client_secret))
        for label, key in candidates:
            expected = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            for header, value in presented:
                if hmac.compare_digest(expected, value):
                    logger.info(
                        "UBER_WEBHOOK_SIGNATURE_VERIFIED: environment=%s key=%s header=%s",
                        env, label, header,
                    )
                    return env
    return None
