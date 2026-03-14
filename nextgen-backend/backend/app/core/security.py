"""
Security utilities — JWT tokens, password hashing, PIN hashing, Meta signature verification.
"""

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from backend.app.core.config import get_settings

settings = get_settings()


# ── Password Hashing (bcrypt) ────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── PIN Hashing (bcrypt — same strength as passwords) ────────────────────────

def hash_pin(pin: str) -> str:
    """Hash a staff PIN using bcrypt."""
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_pin(plain_pin: str, hashed_pin: str) -> bool:
    """Verify a staff PIN against its bcrypt hash."""
    return bcrypt.checkpw(plain_pin.encode("utf-8"), hashed_pin.encode("utf-8"))


# ── Refresh Token Hashing (SHA-256 — fast, one-way) ─────────────────────────

def hash_refresh_token(token: str) -> str:
    """Hash a refresh token with SHA-256 for DB storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── JWT Access Tokens ────────────────────────────────────────────────────────

def create_access_token(
    user_id: uuid.UUID,
    business_id: uuid.UUID | None,
    role: str,
    extra_claims: dict | None = None,
) -> str:
    """
    Create a JWT access token.

    Claims:
        sub: user_id (str)
        bid: business_id (str or null)
        role: user role
        exp: expiration
        iat: issued at
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "bid": str(business_id) if business_id else None,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_admin_access_token(admin_user_id: uuid.UUID, role: str = "SUPER_ADMIN") -> str:
    """Create a JWT access token for admin users (no business_id)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(admin_user_id),
        "bid": None,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "admin_access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and validate a JWT access token.
    Raises jwt.ExpiredSignatureError or jwt.InvalidTokenError on failure.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


# ── Meta Webhook Signature Verification (HMAC SHA256) ────────────────────────

def verify_meta_signature(payload: bytes, signature_header: str) -> bool:
    """
    Verify X-Hub-Signature-256 from Meta webhook.
    signature_header format: 'sha256=<hex_digest>'

    Uses HMAC SHA256 with META_APP_SECRET as the key.
    Uses hmac.compare_digest for timing-safe comparison.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("sha256=", 1)[1]
    computed = hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, expected)
