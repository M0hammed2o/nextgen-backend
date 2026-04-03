"""
Shared fixtures for all tests.

Patches SQLAlchemy's flag_modified so state_machine functions work
without a live database session.
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
