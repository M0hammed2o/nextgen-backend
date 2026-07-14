"""
Credential encryption at rest — Fernet (AES-128-CBC + HMAC).

Used for per-business payment provider credentials (Yoco/PayFast/Stitch/iKhoka
keys) stored on the businesses table.

Storage format: "enc1:<fernet token>". decrypt_credential() passes values
WITHOUT the prefix through unchanged, so legacy plaintext rows keep working
with no data migration — they are re-encrypted the next time the business
saves its settings.

Key comes from CREDENTIALS_ENCRYPTION_KEY (a Fernet key). When unset
(development), encrypt_credential() stores plaintext; production startup
validation refuses to boot without a key.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from backend.app.core.config import get_settings

_PREFIX = "enc1:"


class CredentialDecryptionError(Exception):
    """An encrypted credential could not be decrypted (missing/wrong key)."""


@lru_cache
def _fernet() -> Fernet | None:
    key = get_settings().CREDENTIALS_ENCRYPTION_KEY
    if not key:
        return None
    return Fernet(key.encode("utf-8"))


def encrypt_credential(plain: str | None) -> str | None:
    """Encrypt a credential for storage. None/empty pass through unchanged."""
    if not plain:
        return plain
    f = _fernet()
    if f is None:
        # No key configured (development) — stored as-is.
        return plain
    return _PREFIX + f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_credential(value: str | None) -> str | None:
    """
    Decrypt a stored credential. Values without the enc1: prefix are legacy
    plaintext and returned as-is.
    """
    if not value or not value.startswith(_PREFIX):
        return value
    f = _fernet()
    if f is None:
        raise CredentialDecryptionError(
            "Encrypted credential found but CREDENTIALS_ENCRYPTION_KEY is not configured"
        )
    try:
        return f.decrypt(value[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Failed to decrypt credential — CREDENTIALS_ENCRYPTION_KEY may have changed"
        ) from exc
