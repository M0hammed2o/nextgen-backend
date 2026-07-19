"""
Tests for _validate_production_secrets() — the startup guard that's supposed
to refuse to boot with placeholder/unconfigured secrets in production.

This function had zero test coverage before a real incident: JWT_SECRET_KEY
was set in production to "CHANGE-ME-generate-a-strong-random-string", which
never matched the one exact placeholder string the check looked for
("CHANGE-ME-IN-PRODUCTION"), so the app booted with a forgeable JWT secret
for weeks. These tests lock in the broader detection added to fix that gap.
"""

import pytest

from backend.app import main as main_module


def _valid_secret(seed: str) -> str:
    """A stand-in for a real secrets.token_urlsafe(64)/Fernet-style value."""
    return (seed * 40)[:64]


@pytest.fixture
def settings():
    """The real module-level settings object, saved and restored per test."""
    s = main_module.settings
    original = {
        "ENVIRONMENT": s.ENVIRONMENT,
        "JWT_SECRET_KEY": s.JWT_SECRET_KEY,
        "JWT_ADMIN_SECRET_KEY": s.JWT_ADMIN_SECRET_KEY,
        "CREDENTIALS_ENCRYPTION_KEY": s.CREDENTIALS_ENCRYPTION_KEY,
        "META_APP_SECRET": s.META_APP_SECRET,
        "META_VERIFY_TOKEN": s.META_VERIFY_TOKEN,
        "WHATSAPP_DEFAULT_ACCESS_TOKEN": s.WHATSAPP_DEFAULT_ACCESS_TOKEN,
        "STRIPE_WEBHOOK_SECRET": s.STRIPE_WEBHOOK_SECRET,
    }
    # Start every test from a fully-valid baseline, then break one field at a time.
    s.ENVIRONMENT = "production"
    s.JWT_SECRET_KEY = _valid_secret("a1")
    s.JWT_ADMIN_SECRET_KEY = _valid_secret("b2")
    s.CREDENTIALS_ENCRYPTION_KEY = _valid_secret("c3")
    s.META_APP_SECRET = "a" * 32
    s.META_VERIFY_TOKEN = "a" * 32
    s.WHATSAPP_DEFAULT_ACCESS_TOKEN = "a" * 64
    s.STRIPE_WEBHOOK_SECRET = "whsec_" + "a" * 32
    yield s
    for key, value in original.items():
        setattr(s, key, value)


def test_non_production_environment_skips_all_checks(settings):
    settings.ENVIRONMENT = "development"
    settings.JWT_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
    main_module._validate_production_secrets()  # must not raise


def test_valid_production_config_passes(settings):
    main_module._validate_production_secrets()  # must not raise


def test_exact_legacy_placeholder_still_caught(settings):
    settings.JWT_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        main_module._validate_production_secrets()


def test_differently_worded_placeholder_is_caught(settings):
    """The exact real-incident value: didn't match the old literal check."""
    settings.JWT_SECRET_KEY = "CHANGE-ME-generate-a-strong-random-string"
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        main_module._validate_production_secrets()


def test_admin_placeholder_variant_is_caught(settings):
    settings.JWT_ADMIN_SECRET_KEY = "CHANGE-ME-generate-a-strong-random-string"
    with pytest.raises(RuntimeError, match="JWT_ADMIN_SECRET_KEY"):
        main_module._validate_production_secrets()


def test_short_secret_is_caught_even_without_placeholder_wording(settings):
    settings.JWT_SECRET_KEY = "short"
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        main_module._validate_production_secrets()


def test_matching_business_and_admin_secrets_caught(settings):
    settings.JWT_ADMIN_SECRET_KEY = settings.JWT_SECRET_KEY
    with pytest.raises(RuntimeError, match="must differ"):
        main_module._validate_production_secrets()


def test_missing_credentials_encryption_key_caught(settings):
    settings.CREDENTIALS_ENCRYPTION_KEY = ""
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY"):
        main_module._validate_production_secrets()


def test_placeholder_meta_app_secret_caught(settings):
    settings.META_APP_SECRET = "your-app-secret"
    with pytest.raises(RuntimeError, match="META_APP_SECRET"):
        main_module._validate_production_secrets()


def test_missing_whatsapp_token_caught(settings):
    settings.WHATSAPP_DEFAULT_ACCESS_TOKEN = ""
    with pytest.raises(RuntimeError, match="WHATSAPP_DEFAULT_ACCESS_TOKEN"):
        main_module._validate_production_secrets()


def test_placeholder_stripe_secret_is_a_warning_not_a_crash(settings, caplog):
    """
    Stripe billing isn't live yet (iKhoka is the active payment provider) —
    an unconfigured STRIPE_WEBHOOK_SECRET must not block startup, only warn.
    """
    settings.STRIPE_WEBHOOK_SECRET = "whsec_your_webhook_signing_secret"
    main_module._validate_production_secrets()  # must not raise
    assert any("STRIPE_WEBHOOK_SECRET" in r.message for r in caplog.records)
