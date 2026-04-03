"""
Shared fixtures for all tests.

Patches SQLAlchemy's flag_modified so state_machine functions work
without a live database session.

Also sets required env vars before any imports so pydantic-settings
doesn't fail without a .env.backend file.
"""
import sys
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

# ── Ensure project root is importable ────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Provide minimal env vars so pydantic-settings doesn't need .env.backend ──
# All of these have defaults already, but setting them explicitly avoids any
# validation surprise from a missing file.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/nextgen")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-unit-tests")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_DEFAULT_ACCESS_TOKEN", "test-token")


# ── Silence flag_modified globally so unit tests don't need a real SA session ─
@pytest.fixture(autouse=True)
def _patch_flag_modified():
    with patch("backend.app.bot.state_machine.flag_modified"):
        yield


# ── Minimal ConversationSession mock ──────────────────────────────────────────
class FakeSession:
    """
    A plain-Python stand-in for ConversationSession.
    Supports attribute access exactly like the real model,
    without requiring a DB connection.
    """
    def __init__(self, state: str = "IDLE"):
        self.state = state
        self.context_json: dict = {}


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def building_cart_session():
    return FakeSession(state="BUILDING_CART")
