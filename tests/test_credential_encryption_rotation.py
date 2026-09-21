"""
Wave 0A / Milestone C — CREDENTIALS_ENCRYPTION_KEY rotation (MultiFernet).

Covers the REQUIRED item 1 acceptance criteria:
  - old-key ciphertext decrypts after rotation (primary swapped, old key
    moved into CREDENTIALS_ENCRYPTION_KEYS_OLD)
  - new writes always use the primary key
  - existing (no old keys configured) behaviour is unchanged
  - reencrypt_credential() re-wraps a value under the current primary
"""
import types

import pytest
from cryptography.fernet import Fernet
from unittest.mock import patch

from backend.app.core import crypto


def _settings(primary: str, old: str = ""):
    return types.SimpleNamespace(
        CREDENTIALS_ENCRYPTION_KEY=primary,
        CREDENTIALS_ENCRYPTION_KEYS_OLD=old,
    )


@pytest.fixture(autouse=True)
def _clear_fernet_cache():
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def test_old_key_ciphertext_decrypts_after_rotation():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    with patch.object(crypto, "get_settings", return_value=_settings(old_key)):
        crypto._fernet.cache_clear()
        encrypted = crypto.encrypt_credential("secret-value")

    # Rotate: new_key becomes primary, old_key moves to the OLD list.
    with patch.object(crypto, "get_settings", return_value=_settings(new_key, old_key)):
        crypto._fernet.cache_clear()
        assert crypto.decrypt_credential(encrypted) == "secret-value"


def test_new_writes_use_primary_key_not_an_old_one():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    with patch.object(crypto, "get_settings", return_value=_settings(new_key, old_key)):
        crypto._fernet.cache_clear()
        encrypted = crypto.encrypt_credential("another-secret")
        assert crypto.decrypt_credential(encrypted) == "another-secret"

    # If the old key were used for new writes, decrypting with ONLY the old
    # key (as primary, no OLD list) would succeed. It must NOT.
    with patch.object(crypto, "get_settings", return_value=_settings(old_key)):
        crypto._fernet.cache_clear()
        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt_credential(encrypted)


def test_no_old_keys_configured_behaviour_unchanged():
    key = Fernet.generate_key().decode()
    with patch.object(crypto, "get_settings", return_value=_settings(key)):
        crypto._fernet.cache_clear()
        encrypted = crypto.encrypt_credential("plain-round-trip")
        assert encrypted.startswith("enc1:")
        assert crypto.decrypt_credential(encrypted) == "plain-round-trip"
        assert crypto.decrypt_credential(None) is None
        assert crypto.decrypt_credential("") == ""
        # Legacy plaintext (no enc1: prefix) passes through unchanged.
        assert crypto.decrypt_credential("legacy-plaintext-value") == "legacy-plaintext-value"


def test_development_no_key_configured_stores_plaintext():
    with patch.object(crypto, "get_settings", return_value=_settings("")):
        crypto._fernet.cache_clear()
        assert crypto.encrypt_credential("dev-value") == "dev-value"
        assert crypto.decrypt_credential("dev-value") == "dev-value"


def test_reencrypt_credential_rewraps_under_current_primary():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    with patch.object(crypto, "get_settings", return_value=_settings(old_key)):
        crypto._fernet.cache_clear()
        encrypted_old = crypto.encrypt_credential("rewrap-me")

    with patch.object(crypto, "get_settings", return_value=_settings(new_key, old_key)):
        crypto._fernet.cache_clear()
        rewrapped = crypto.reencrypt_credential(encrypted_old)
        assert rewrapped != encrypted_old
        assert crypto.decrypt_credential(rewrapped) == "rewrap-me"

    # After rewrap, decrypting with ONLY the new key (old key fully retired)
    # must still work — proving the value is now encrypted under new_key.
    with patch.object(crypto, "get_settings", return_value=_settings(new_key)):
        crypto._fernet.cache_clear()
        assert crypto.decrypt_credential(rewrapped) == "rewrap-me"


def test_reencrypt_credential_noop_for_none_and_plaintext():
    key = Fernet.generate_key().decode()
    with patch.object(crypto, "get_settings", return_value=_settings(key)):
        crypto._fernet.cache_clear()
        assert crypto.reencrypt_credential(None) is None
        assert crypto.reencrypt_credential("") == ""
