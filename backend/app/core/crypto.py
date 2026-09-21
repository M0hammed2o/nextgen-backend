"""
Credential encryption at rest — Fernet (AES-128-CBC + HMAC).

Used for per-business payment provider credentials (Yoco/PayFast/Stitch/iKhoka
keys) stored on the businesses table, and (as of Wave 0A / Milestone C /
DECISION-0022) per-business WhatsApp Embedded Signup access tokens and
phone-registration PINs stored on `whatsapp_connections`.

Storage format: "enc1:<fernet token>". decrypt_credential() passes values
WITHOUT the prefix through unchanged, so legacy plaintext rows keep working
with no data migration — they are re-encrypted the next time the business
saves its settings.

Key comes from CREDENTIALS_ENCRYPTION_KEY (a Fernet key). When unset
(development), encrypt_credential() stores plaintext; production startup
validation refuses to boot without a key.

── Key rotation (MultiFernet) ────────────────────────────────────────────────
CREDENTIALS_ENCRYPTION_KEY is always the PRIMARY key — every new
encrypt_credential() call uses it, and it is tried FIRST on decrypt.
CREDENTIALS_ENCRYPTION_KEYS_OLD is an optional comma-separated list of
previously-used Fernet keys — decrypt-only, never used to encrypt. This lets
an operator rotate the primary key (e.g. after a suspected leak, or ahead of
issuing long-lived per-business WhatsApp tokens) without breaking every
row encrypted under the old key: move the old key into
CREDENTIALS_ENCRYPTION_KEYS_OLD, set a new CREDENTIALS_ENCRYPTION_KEYS
primary, and existing ciphertext keeps decrypting. `reencrypt_credential()`
re-wraps a value under the current primary so a row can be migrated forward
opportunistically (e.g. next time a business saves its settings), the same
pattern the "legacy plaintext" comment above already establishes for the
non-rotation case.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from backend.app.core.config import get_settings

_PREFIX = "enc1:"


class CredentialDecryptionError(Exception):
    """An encrypted credential could not be decrypted (missing/wrong key)."""


@lru_cache
def _fernet() -> MultiFernet | None:
    """
    Build a MultiFernet over [primary, *old keys]. MultiFernet.encrypt()
    always uses the FIRST key in the list (the primary); .decrypt() tries
    each key in order, so old ciphertext keeps decrypting after rotation.

    Returns None only when no primary key is configured at all (development
    default) — old keys alone, with no primary, is still treated as
    "encryption disabled" for new writes (encrypt_credential falls back to
    plaintext), matching existing unrotated behaviour exactly when
    CREDENTIALS_ENCRYPTION_KEYS_OLD is unset.
    """
    settings = get_settings()
    primary = settings.CREDENTIALS_ENCRYPTION_KEY
    if not primary:
        return None
    keys = [Fernet(primary.encode("utf-8"))]
    old_raw = getattr(settings, "CREDENTIALS_ENCRYPTION_KEYS_OLD", "") or ""
    for old_key in old_raw.split(","):
        old_key = old_key.strip()
        if old_key:
            keys.append(Fernet(old_key.encode("utf-8")))
    return MultiFernet(keys)


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
    plaintext and returned as-is. Tries the primary key first, then any
    configured old keys (CREDENTIALS_ENCRYPTION_KEYS_OLD) — see module
    docstring for the rotation model.
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
            "Failed to decrypt credential — CREDENTIALS_ENCRYPTION_KEY may have changed "
            "and no configured CREDENTIALS_ENCRYPTION_KEYS_OLD entry can decrypt it either"
        ) from exc


def reencrypt_credential(value: str | None) -> str | None:
    """
    Re-encrypt a stored credential under the current PRIMARY key.

    Decrypts with decrypt_credential() (primary or any old key) then
    re-encrypts with encrypt_credential() (always primary). No-op for
    None/empty/legacy-plaintext values, matching encrypt_credential's own
    pass-through behaviour. Use this to migrate a row forward after a key
    rotation, or immediately after any decrypt in a hot path where the
    caller wants to opportunistically re-wrap old-key ciphertext.
    """
    if not value:
        return value
    plain = decrypt_credential(value)
    return encrypt_credential(plain)
