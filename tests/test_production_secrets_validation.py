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
        "BILLING_PROVIDER": s.BILLING_PROVIDER,
        "BILLING_ENABLED": s.BILLING_ENABLED,
        "PAYFAST_BILLING_MERCHANT_ID": s.PAYFAST_BILLING_MERCHANT_ID,
        "PAYFAST_BILLING_MERCHANT_KEY": s.PAYFAST_BILLING_MERCHANT_KEY,
        "PAYFAST_BILLING_PASSPHRASE": s.PAYFAST_BILLING_PASSPHRASE,
        "UBER_EATS_WEBHOOK_ENABLED": s.UBER_EATS_WEBHOOK_ENABLED,
        "UBER_EATS_CLIENT_ID": s.UBER_EATS_CLIENT_ID,
        "UBER_EATS_CLIENT_SECRET": s.UBER_EATS_CLIENT_SECRET,
        "SQL_ECHO_ENABLED": s.SQL_ECHO_ENABLED,
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
    s.BILLING_PROVIDER = "STRIPE"
    s.BILLING_ENABLED = True
    s.PAYFAST_BILLING_MERCHANT_ID = "10000100"
    s.PAYFAST_BILLING_MERCHANT_KEY = "46f0cd694581a"
    s.PAYFAST_BILLING_PASSPHRASE = _valid_secret("d4")
    s.UBER_EATS_WEBHOOK_ENABLED = True
    s.UBER_EATS_CLIENT_ID = _valid_secret("e5")
    s.UBER_EATS_CLIENT_SECRET = _valid_secret("f6")
    s.SQL_ECHO_ENABLED = False
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


def test_placeholder_stripe_secret_is_caught(settings):
    """
    Stripe self-service subscription billing went live 2026-07-21 — an
    unconfigured STRIPE_WEBHOOK_SECRET is now a hard production failure,
    same as every other secret (previously a non-fatal warning while
    Stripe billing was dormant; see KNOWN_BUGS.md BUG-0001).
    """
    settings.STRIPE_WEBHOOK_SECRET = "whsec_your_webhook_signing_secret"
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        main_module._validate_production_secrets()


def test_stripe_placeholder_ignored_when_payfast_is_the_active_provider(settings):
    """
    BUG (found 2026-07-27 via a real Render deploy failure): this check used
    to demand a real STRIPE_WEBHOOK_SECRET unconditionally, even when
    BILLING_PROVIDER=PAYFAST and Stripe isn't in use at all. Switching
    providers must not require configuring the OTHER provider's secrets.
    """
    settings.BILLING_PROVIDER = "PAYFAST"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_your_webhook_signing_secret"  # deliberately a placeholder
    main_module._validate_production_secrets()  # must not raise


def test_payfast_placeholder_passphrase_is_caught_when_active(settings):
    settings.BILLING_PROVIDER = "PAYFAST"
    settings.PAYFAST_BILLING_PASSPHRASE = "your-passphrase-here"
    with pytest.raises(RuntimeError, match="PAYFAST_BILLING_PASSPHRASE"):
        main_module._validate_production_secrets()


def test_payfast_missing_merchant_credentials_caught_when_active(settings):
    settings.BILLING_PROVIDER = "PAYFAST"
    settings.PAYFAST_BILLING_MERCHANT_ID = ""
    with pytest.raises(RuntimeError, match="PAYFAST_BILLING_MERCHANT_ID"):
        main_module._validate_production_secrets()


def test_valid_payfast_production_config_passes(settings):
    settings.BILLING_PROVIDER = "PAYFAST"
    main_module._validate_production_secrets()  # must not raise


def test_billing_disabled_skips_all_billing_secret_checks(settings):
    """
    BILLING_ENABLED=false lets an owner defer NextGen's own SaaS billing
    entirely (e.g. before real PayFast merchant credentials are ready)
    without weakening any other production secret check.
    """
    settings.BILLING_ENABLED = False
    settings.BILLING_PROVIDER = "PAYFAST"
    settings.PAYFAST_BILLING_MERCHANT_ID = ""
    settings.PAYFAST_BILLING_MERCHANT_KEY = ""
    settings.PAYFAST_BILLING_PASSPHRASE = ""
    settings.STRIPE_WEBHOOK_SECRET = ""
    main_module._validate_production_secrets()  # must not raise


def test_billing_disabled_still_enforces_every_other_secret(settings):
    """The escape hatch must be billing-specific, not a blanket bypass."""
    settings.BILLING_ENABLED = False
    settings.JWT_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        main_module._validate_production_secrets()


# ── BUG-0089: UBER_EATS_CLIENT_SECRET/CLIENT_ID must be required when the ──
# ── Uber Eats webhook is enabled — the exact BUG-0001 class of gap, on a ───
# ── new webhook, recurring because it wasn't generalised the first time. ──

def test_missing_uber_client_secret_caught_when_webhook_enabled(settings):
    """
    The real-world consequence: verify_signature() fails closed on an empty
    secret, so every genuine, correctly-signed Uber order is logged
    BAD_SIGNATURE and acked HTTP 200 — Uber never retries, and the order is
    permanently, silently lost. This must be a hard boot failure, not a
    silent runtime discard.
    """
    settings.UBER_EATS_CLIENT_SECRET = ""
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_SECRET"):
        main_module._validate_production_secrets()


def test_placeholder_uber_client_secret_caught_when_webhook_enabled(settings):
    settings.UBER_EATS_CLIENT_SECRET = "CHANGE-ME"
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_SECRET"):
        main_module._validate_production_secrets()


def test_missing_uber_client_id_caught_when_webhook_enabled(settings):
    settings.UBER_EATS_CLIENT_ID = ""
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_ID"):
        main_module._validate_production_secrets()


def test_missing_uber_secrets_ignored_when_webhook_disabled(settings):
    """The platform-wide Uber Eats kill switch legitimately means 'not configured yet'."""
    settings.UBER_EATS_WEBHOOK_ENABLED = False
    settings.UBER_EATS_CLIENT_ID = ""
    settings.UBER_EATS_CLIENT_SECRET = ""
    main_module._validate_production_secrets()  # must not raise


def test_valid_uber_production_config_passes(settings):
    main_module._validate_production_secrets()  # must not raise (fixture baseline)


# ── BUG-0085: SQL_ECHO_ENABLED must fail closed in production/staging — ────
# ── SQLAlchemy `echo` logs bound parameters verbatim, including real ───────
# ── customer PII (DigitalReceipt.phone_number_e164, Customer.wa_id). ───────

def test_sql_echo_enabled_is_rejected_in_production(settings):
    settings.SQL_ECHO_ENABLED = True
    with pytest.raises(RuntimeError, match="SQL_ECHO_ENABLED"):
        main_module._validate_production_secrets()


def test_sql_echo_enabled_is_rejected_in_staging_too(settings):
    """
    Staging is validated exactly like production (see this function's own
    docstring) — a staging deploy with real/real-looking customer data must
    not be allowed to echo it either.
    """
    settings.ENVIRONMENT = "staging"
    settings.SQL_ECHO_ENABLED = True
    with pytest.raises(RuntimeError, match="SQL_ECHO_ENABLED"):
        main_module._validate_production_secrets()


def test_sql_echo_enabled_is_allowed_in_development(settings):
    """
    The whole point of decoupling SQL_ECHO_ENABLED from DEBUG (BUG-0085) is
    that local SQL-query debugging must still be possible — just via its own
    explicit flag rather than implicitly through DEBUG.
    """
    settings.ENVIRONMENT = "development"
    settings.SQL_ECHO_ENABLED = True
    main_module._validate_production_secrets()  # must not raise


def test_sql_echo_disabled_production_config_passes(settings):
    settings.SQL_ECHO_ENABLED = False
    main_module._validate_production_secrets()  # must not raise (fixture baseline)


def test_debug_true_alone_does_not_trigger_the_sql_echo_check(settings):
    """
    DEBUG and SQL_ECHO_ENABLED are deliberately independent settings — DEBUG
    being true (e.g. for verbose app-level logging) must not, on its own,
    fail production boot. Only SQL_ECHO_ENABLED does.
    """
    settings.DEBUG = True
    settings.SQL_ECHO_ENABLED = False
    main_module._validate_production_secrets()  # must not raise


async def test_lifespan_actually_refuses_to_boot_with_sql_echo_in_production(settings):
    """
    Proves the FAIL-CLOSED behaviour at the real boot path (the FastAPI
    `lifespan` context manager main.py's ASGI server actually calls on
    startup), not just the validator function called directly in isolation.
    """
    settings.SQL_ECHO_ENABLED = True
    with pytest.raises(RuntimeError, match="SQL_ECHO_ENABLED"):
        async with main_module.lifespan(main_module.app):
            pytest.fail("lifespan must raise before yielding when SQL_ECHO_ENABLED is true in production")
