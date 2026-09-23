"""
BUG-0085: backend/app/db/session.py's SQLAlchemy engine must be wired to the
dedicated `SQL_ECHO_ENABLED` setting, never to `DEBUG` — DEBUG being true
must NOT enable SQL echo, and SQL_ECHO_ENABLED being true must actually
reach the real `engine` object (not just the setting in isolation).

The engine is constructed once at module import time, so proving the wiring
requires reloading backend.app.db.session under each settings combination
rather than just asserting on the Settings object.
"""

import importlib

from backend.app.core.config import get_settings


def _reload_session_with(monkeypatch, *, sql_echo: bool, debug: bool):
    monkeypatch.setenv("SQL_ECHO_ENABLED", "true" if sql_echo else "false")
    monkeypatch.setenv("DEBUG", "true" if debug else "false")
    get_settings.cache_clear()
    import backend.app.db.session as session_module
    return importlib.reload(session_module)


def test_sql_echo_enabled_true_reaches_the_real_engine(monkeypatch):
    session_module = _reload_session_with(monkeypatch, sql_echo=True, debug=False)
    try:
        assert session_module.engine.echo is True
    finally:
        # Restore a clean module state for any test that runs after this one.
        _reload_session_with(monkeypatch, sql_echo=False, debug=False)
        get_settings.cache_clear()


def test_debug_true_alone_does_not_enable_engine_echo(monkeypatch):
    """The actual regression this bug was filed over: DEBUG=true must not echo SQL."""
    session_module = _reload_session_with(monkeypatch, sql_echo=False, debug=True)
    try:
        assert session_module.engine.echo is False
    finally:
        _reload_session_with(monkeypatch, sql_echo=False, debug=False)
        get_settings.cache_clear()


def test_default_settings_have_sql_echo_disabled():
    assert get_settings().SQL_ECHO_ENABLED is False
