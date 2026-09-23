"""
Fix group 1 (wave0a/sec) — ENVIRONMENT fail-closed.

ENVIRONMENT has no default and is validated against an explicit allow-list
in both backend.app.core.config.Settings and admin_api.app.core.config.
AdminSettings. A missing or unknown value must fail at boot rather than
silently behaving like "development" (which skips secret validation and
webhook signature checks — see KNOWN_BUGS.md BUG-0014).
"""
import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings, VALID_ENVIRONMENTS, _looks_like_placeholder


_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost/nextgen",
    "REDIS_URL": "redis://localhost:6379/0",
    "JWT_SECRET_KEY": "test-secret-key-for-unit-tests",
    "JWT_ADMIN_SECRET_KEY": "test-admin-secret-key-for-unit-tests",
    "META_VERIFY_TOKEN": "test-verify-token",
    "WHATSAPP_DEFAULT_ACCESS_TOKEN": "test-token",
}


def _settings_with_env(**overrides) -> Settings:
    env = {**_REQUIRED_ENV, **overrides}
    with patch.dict(os.environ, env, clear=False):
        return Settings(_env_file=None)


# ── Backend Settings ─────────────────────────────────────────────────────────

def test_missing_environment_fails_to_boot():
    """No ENVIRONMENT set at all (and no .env file) → ValidationError, not a silent default."""
    with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
        os.environ.pop("ENVIRONMENT", None)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_unknown_environment_value_rejected():
    with pytest.raises(ValidationError):
        _settings_with_env(ENVIRONMENT="qa-environment-3")


@pytest.mark.parametrize("env", sorted(VALID_ENVIRONMENTS))
def test_every_allow_listed_environment_boots(env):
    s = _settings_with_env(ENVIRONMENT=env)
    assert s.ENVIRONMENT == env


def test_development_and_test_are_the_only_skip_values():
    """VALID_ENVIRONMENTS must be exactly these four — no silent extra values."""
    assert VALID_ENVIRONMENTS == {"development", "staging", "production", "test"}


# ── admin_api AdminSettings ───────────────────────────────────────────────────

def test_admin_settings_missing_environment_fails_to_boot():
    from admin_api.app.core.config import AdminSettings

    with patch.dict(os.environ, _REQUIRED_ENV, clear=True):
        os.environ.pop("ENVIRONMENT", None)
        with pytest.raises(ValidationError):
            AdminSettings(_env_file=None)


def test_admin_settings_unknown_environment_rejected():
    from admin_api.app.core.config import AdminSettings

    with patch.dict(os.environ, {**_REQUIRED_ENV, "ENVIRONMENT": "bogus"}, clear=False):
        with pytest.raises(ValidationError):
            AdminSettings(_env_file=None)


def test_admin_settings_valid_environment_boots():
    from admin_api.app.core.config import AdminSettings

    with patch.dict(os.environ, {**_REQUIRED_ENV, "ENVIRONMENT": "staging"}, clear=False):
        s = AdminSettings(_env_file=None)
    assert s.ENVIRONMENT == "staging"


# ── _validate_production_secrets: staging validated like production ─────────

def test_secret_validation_skipped_for_development():
    from backend.app import main as backend_main

    with patch.object(backend_main, "settings") as mock_settings:
        mock_settings.ENVIRONMENT = "development"
        backend_main._validate_production_secrets()  # must not raise


def test_secret_validation_skipped_for_test_env():
    from backend.app import main as backend_main

    with patch.object(backend_main, "settings") as mock_settings:
        mock_settings.ENVIRONMENT = "test"
        backend_main._validate_production_secrets()  # must not raise


def test_secret_validation_runs_for_staging_and_rejects_placeholders():
    """
    Staging must be validated exactly like production — this was the actual
    gap fix group 1 closes (previously only ENVIRONMENT=="production" ran
    this check at all).
    """
    from backend.app import main as backend_main

    with patch.object(backend_main, "settings") as mock_settings:
        mock_settings.ENVIRONMENT = "staging"
        mock_settings.JWT_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
        mock_settings.JWT_ADMIN_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
        mock_settings.CREDENTIALS_ENCRYPTION_KEY = ""
        mock_settings.META_APP_SECRET = "CHANGE-ME"
        mock_settings.META_VERIFY_TOKEN = "CHANGE-ME"
        mock_settings.WHATSAPP_DEFAULT_ACCESS_TOKEN = "CHANGE-ME"
        mock_settings.BILLING_ENABLED = False

        with pytest.raises(RuntimeError, match="staging deployment"):
            backend_main._validate_production_secrets()


def test_secret_validation_runs_for_production_still():
    from backend.app import main as backend_main

    with patch.object(backend_main, "settings") as mock_settings:
        mock_settings.ENVIRONMENT = "production"
        mock_settings.JWT_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
        mock_settings.JWT_ADMIN_SECRET_KEY = "CHANGE-ME-IN-PRODUCTION"
        mock_settings.CREDENTIALS_ENCRYPTION_KEY = ""
        mock_settings.META_APP_SECRET = "CHANGE-ME"
        mock_settings.META_VERIFY_TOKEN = "CHANGE-ME"
        mock_settings.WHATSAPP_DEFAULT_ACCESS_TOKEN = "CHANGE-ME"
        mock_settings.BILLING_ENABLED = False

        with pytest.raises(RuntimeError, match="production deployment"):
            backend_main._validate_production_secrets()


# ── admin_api: placeholder parity via shared _looks_like_placeholder ────────

def test_admin_api_rejects_historical_non_exact_placeholder():
    """
    The historical incident: JWT_SECRET_KEY = "CHANGE-ME-generate-a-strong-
    random-string" is NOT equal to the old literal "CHANGE-ME-IN-PRODUCTION"
    check, but IS a placeholder — admin_api must now reject it too, via the
    same _looks_like_placeholder() the backend uses (imported, not duplicated).
    """
    from admin_api.app import main as admin_main

    fake_backend_settings = type("S", (), {
        "ENVIRONMENT": "production",
        "JWT_SECRET_KEY": "CHANGE-ME-generate-a-strong-random-string",
        "JWT_ADMIN_SECRET_KEY": "a-real-64-char-admin-secret-1234567890123456789012345",
        "SQL_ECHO_ENABLED": False,
    })()

    with patch("backend.app.core.config.get_settings", return_value=fake_backend_settings):
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY looks like a placeholder"):
            admin_main._validate_production_secrets()


def test_admin_api_accepts_real_looking_secrets():
    from admin_api.app import main as admin_main

    fake_backend_settings = type("S", (), {
        "ENVIRONMENT": "production",
        "JWT_SECRET_KEY": "a-real-64-char-business-secret-1234567890123456789012",
        "JWT_ADMIN_SECRET_KEY": "a-real-64-char-admin-secret-1234567890123456789012345",
        "SQL_ECHO_ENABLED": False,
    })()

    with patch("backend.app.core.config.get_settings", return_value=fake_backend_settings):
        admin_main._validate_production_secrets()  # must not raise


def test_admin_api_skips_validation_in_development():
    from admin_api.app import main as admin_main

    fake_backend_settings = type("S", (), {
        "ENVIRONMENT": "development",
        "JWT_SECRET_KEY": "CHANGE-ME-IN-PRODUCTION",
        "JWT_ADMIN_SECRET_KEY": "CHANGE-ME-IN-PRODUCTION",
    })()

    with patch("backend.app.core.config.get_settings", return_value=fake_backend_settings):
        admin_main._validate_production_secrets()  # must not raise


# ── BUG-0085: admin_api must ALSO refuse SQL_ECHO_ENABLED in production ─────
# ── (it shares backend's engine object but boots as its own process, so ────
# ── backend/app/main.py's own lifespan never runs here). ───────────────────

def test_admin_api_rejects_sql_echo_enabled_in_production():
    from admin_api.app import main as admin_main

    fake_backend_settings = type("S", (), {
        "ENVIRONMENT": "production",
        "JWT_SECRET_KEY": "a-real-64-char-business-secret-1234567890123456789012",
        "JWT_ADMIN_SECRET_KEY": "a-real-64-char-admin-secret-1234567890123456789012345",
        "SQL_ECHO_ENABLED": True,
    })()

    with patch("backend.app.core.config.get_settings", return_value=fake_backend_settings):
        with pytest.raises(RuntimeError, match="SQL_ECHO_ENABLED"):
            admin_main._validate_production_secrets()


def test_admin_api_allows_sql_echo_enabled_in_development():
    from admin_api.app import main as admin_main

    fake_backend_settings = type("S", (), {
        "ENVIRONMENT": "development",
        "JWT_SECRET_KEY": "CHANGE-ME-IN-PRODUCTION",
        "JWT_ADMIN_SECRET_KEY": "CHANGE-ME-IN-PRODUCTION",
        "SQL_ECHO_ENABLED": True,
    })()

    with patch("backend.app.core.config.get_settings", return_value=fake_backend_settings):
        admin_main._validate_production_secrets()  # must not raise — dev skips entirely


# ── _looks_like_placeholder itself (shared helper) ───────────────────────────

@pytest.mark.parametrize("value", [
    "", "CHANGE-ME", "CHANGE-ME-IN-PRODUCTION", "your-app-secret",
    "CHANGE-ME-generate-a-strong-random-string", "short",
])
def test_looks_like_placeholder_true(value):
    assert _looks_like_placeholder(value) is True


def test_looks_like_placeholder_false_for_real_secret():
    real = "8f3a1c9e7b2d4f6a0c8e1b3d5f7a9c1e3b5d7f9a1c3e5b7d9f1a3c5e7b9d1f3a"
    assert _looks_like_placeholder(real) is False
