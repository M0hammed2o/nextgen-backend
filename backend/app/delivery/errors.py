"""
Delivery-module exception types.

Kept deliberately small and specific — a handler catching
`DeliveryProviderNotImplemented` (a disabled/stub provider, e.g. Mr D) must
never be able to accidentally also swallow `DeliveryCapabilityUnsupported`
(a real provider being asked to do something it genuinely can't) or a
transport-level `DeliveryProviderTimeout`, since those three call for
different responses (refuse cleanly / refuse cleanly / retry).
"""

from __future__ import annotations


class DeliveryError(Exception):
    """Base class for every exception this module raises deliberately."""


class DeliveryCapabilityUnsupported(DeliveryError):
    """
    Raised by `DeliveryProviderAdapter._require()` when a capability flag
    for the calling adapter is False. Enforced by the ABC itself (see
    `base.py`) so a subclass can never forget the check by calling the
    network method directly.
    """

    def __init__(self, provider: str, capability: str):
        self.provider = provider
        self.capability = capability
        super().__init__(f"{provider} does not support capability: {capability}")


class DeliveryProviderNotImplemented(DeliveryError):
    """
    Raised by every network method on a disabled stub adapter (currently
    `MrDAdapter` — see `mrd/adapter.py`). Distinct from
    `DeliveryCapabilityUnsupported` so a caller can tell "this adapter is a
    real, live integration that doesn't do X" apart from "this adapter is
    not live at all, full stop."
    """

    def __init__(self, provider: str, method: str):
        self.provider = provider
        self.method = method
        super().__init__(f"{provider} adapter is a disabled stub: {method} is not implemented")


class DeliveryProviderTimeout(DeliveryError):
    """A network call to the provider timed out. Retryable by the caller."""


class DeliveryProviderError(DeliveryError):
    """
    A network call to the provider returned an error response (4xx/5xx) that
    isn't a timeout. Carries the provider's own status code where known, so
    callers can distinguish "retry later" from "this will never succeed."
    """

    def __init__(self, provider: str, message: str, status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"{provider} error ({status_code}): {message}")


class DeliveryTenantResolutionError(DeliveryError):
    """
    Raised internally when a caller asks for a business_id resolution
    without going through `tenant_resolver.py`'s single sanctioned path.
    Not expected to ever surface in normal operation — a defence against a
    future accidental bypass of the trust boundary, not a routine failure
    mode.
    """
