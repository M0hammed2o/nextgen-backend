"""
Vendor self-registration code (souk).

One shared, human-friendly code per event, handed to vendors by the
organiser. Only a bcrypt hash is stored (events.registration_code_hash).

Why bcrypt rather than SHA-256: the code is short (8 characters from a
31-symbol alphabet, about 40 bits), so a leaked fast hash could be brute
forced offline in seconds; bcrypt's cost makes that impractical and makes
each online guess expensive. Online guessing is bounded by the per-IP rate
limit on the public route. bcrypt.checkpw is a constant-time comparison.

``verify_code`` always performs exactly one bcrypt check, against a dummy
hash when the event has no code, so "no code / closed" and "wrong code" take
the same time.
"""
from __future__ import annotations

import secrets

from starlette.concurrency import run_in_threadpool

from backend.app.core.security import hash_password, verify_password

# No 0/O, 1/I/L: a code is read aloud and typed on a phone.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

_DUMMY_HASH = hash_password("dummy-registration-code-not-a-secret")


def normalise_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


async def hash_code(code: str) -> str:
    return await run_in_threadpool(hash_password, normalise_code(code))


async def verify_code(candidate: str, code_hash: str | None) -> bool:
    """One bcrypt check regardless of whether the event has a code."""
    ok = await run_in_threadpool(verify_password, normalise_code(candidate), code_hash or _DUMMY_HASH)
    return bool(ok and code_hash)
