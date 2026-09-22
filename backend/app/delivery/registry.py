"""
Delivery provider registry — maps provider names to adapter instances, and
declares which providers the public webhook route will ever act on.

`_WEBHOOK_ENABLED_PROVIDERS` IS THE ONLY PLACE THAT MATTERS FOR MR D SAFETY
------------------------------------------------------------------------------
No real Mr D endpoint, header, or auth scheme is publicly documented (see
`docs/MRD_API_ACCESS_2026-10.md`), so `MrDAdapter` is a disabled stub whose
own capability flags are all False and whose network methods all raise —
but that alone would still let a malicious or mistaken caller reach
`get_provider("MR_D")` and get back a real (if inert) adapter object. The
webhook route (`routes_delivery_webhook.py`) checks path membership in
`_WEBHOOK_ENABLED_PROVIDERS` BEFORE calling `get_provider()` at all, so no
request shaped like a Mr D webhook can reach adapter code, database lookups,
or logging of any kind beyond a flat 200/`error_category=UNSUPPORTED_PROVIDER`
ack. `MR_D` is deliberately absent from this set — do not add it without an
explicit `DECISIONS.md` entry once a real, documented Mr D integration
exists to justify it.
"""

from __future__ import annotations

from backend.app.delivery.base import DeliveryProviderAdapter
from backend.app.delivery.mrd.adapter import MrDAdapter
from backend.app.delivery.uber.adapter import UberEatsAdapter

_ADAPTERS: dict[str, DeliveryProviderAdapter] = {
    "UBER_EATS": UberEatsAdapter(),
    "MR_D": MrDAdapter(),
}

#: Providers whose inbound webhook the public route will actually process.
#: MR_D IS DELIBERATELY ABSENT — see module docstring.
_WEBHOOK_ENABLED_PROVIDERS: frozenset[str] = frozenset({"UBER_EATS"})


def get_provider(provider_name: str | None) -> DeliveryProviderAdapter | None:
    """Return the adapter instance for the given provider name, or None if unknown."""
    if not provider_name:
        return None
    return _ADAPTERS.get(provider_name.upper())


def is_webhook_enabled(provider_name: str | None) -> bool:
    """
    True only for a provider this platform will actually act on inbound
    webhooks for. Checked BEFORE `get_provider()` in the webhook route, so a
    request for a known-but-disabled provider (MR_D) never reaches adapter
    code at all.
    """
    return bool(provider_name) and provider_name.upper() in _WEBHOOK_ENABLED_PROVIDERS
