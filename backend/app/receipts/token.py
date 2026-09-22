"""
Public receipt token generation.

The token is the ENTIRE lookup key for the unauthenticated public receipt
page (GET /v1/r/{token}, backend/app/api/v1/routes_receipt_public.py) — it
must never be derivable from, or correlated with, the order/business/
customer it points at.

ENTROPY
-------
`secrets.token_urlsafe(32)` draws 32 bytes (256 bits) from the OS CSPRNG and
base64url-encodes them (~43 characters, no padding). 256 bits is far beyond
the commonly-cited 128-bit minimum for an unguessable public token — at that
size, even a determined attacker sending a sustained 1,000 requests/second
would need on the order of 10^68 years of uniform random guessing before a
50% chance of ever hitting one valid token, several orders of magnitude
longer than the age of the universe. Collision risk between two GENERATED
tokens is likewise negligible at any realistic platform volume (a birthday-
bound collision across even a billion issued tokens is still astronomically
unlikely against a 256-bit space) — but "astronomically unlikely" is not
"impossible", so the DB's own UNIQUE constraint on digital_receipts.token is
still the authoritative guarantee, and the caller (see
backend/app/receipts/whatsapp_receipt.py::_get_or_create_receipt) retries
generation once on a unique-violation rather than assuming this function
can never collide.
"""

import secrets


def generate_receipt_token() -> str:
    """A cryptographically random, URL-safe, ~256-bit public receipt token."""
    return secrets.token_urlsafe(32)
