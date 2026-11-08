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
which the new key moves into `X-Uber-Signature`. The first real Uber sandbox
event (2026-11-07) settled it: signed with the dashboard signing key in
`X-Uber-Signature`. So an environment's dashboard keys, when configured, are
the only keys accepted (both headers are tried); the client secret is only a
fallback for an environment without them (production, until its signing-key
settings exist). The label of the key that matched (never its value) is
logged.
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

    Candidates per environment: its dashboard signing keys when any are
    configured, otherwise its client secret (the key the Eats guide
    documents). The first authentic Uber sandbox event (2026-11-07,
    `store.provisioned`) was signed with the dashboard signing key, so once an
    environment has signing keys its client secret is no longer accepted —
    one fewer key that can forge a webhook. Production has no signing-key
    settings yet and keeps the client secret until it does.

    Each candidate is tried against `X-Uber-Signature` and
    `X-Uber-Signature-New`. Production is tried first. Malformed or missing
    headers never match. Constant-time comparison. On success the log names,
    for every presented header, the label of the key that verified it (or
    `none`) — never a value — so a real event also shows how Uber uses the
    rotation header.
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
        if not candidates and config.client_secret:
            candidates.append(("client_secret", config.client_secret))
        expected = [
            (label, hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest())
            for label, key in candidates
        ]
        matched = {
            header: next((label for label, digest in expected if hmac.compare_digest(digest, value)), None)
            for header, value in presented
        }
        if any(matched.values()):
            header, label = next((h, k) for h, k in matched.items() if k)
            logger.info(
                "UBER_WEBHOOK_SIGNATURE_VERIFIED: environment=%s key=%s header=%s headers={%s}",
                env, label, header, ", ".join(f"{h}={k or 'none'}" for h, k in matched.items()),
            )
            return env
    # Refused. If Uber turns out to sign some events with the client secret
    # after all (the Eats guide says it does), say so loudly instead of
    # dropping them silently — still refused, never accepted.
    for env in ENVIRONMENTS:
        config = get_environment_config(env)
        if config.webhook_signing_keys and config.client_secret:
            digest = hmac.new(config.client_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            if any(hmac.compare_digest(digest, value) for _, value in presented):
                logger.warning(
                    "UBER_WEBHOOK_SIGNED_WITH_CLIENT_SECRET_REFUSED: environment=%s — this event is signed "
                    "with the client secret, which is not accepted while dashboard signing keys are configured",
                    env,
                )
    return None
