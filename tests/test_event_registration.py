"""Unit tests for the souk registration-code helpers and the public request schema."""
import asyncio

import pytest
from pydantic import ValidationError

from backend.app.api.v1.routes_event_public import RegisterRequest
from backend.app.events import registration as reg


def test_generated_codes_use_the_unambiguous_alphabet_and_are_random():
    codes = {reg.generate_code() for _ in range(200)}
    assert len(codes) > 190
    for c in codes:
        assert len(c) == reg.CODE_LENGTH and set(c) <= set(reg.ALPHABET)
    assert not set(reg.ALPHABET) & set("01OIL")


def test_display_and_normalise_round_trip():
    c = reg.generate_code()
    assert reg.normalise_code(reg.display_code(c).lower() + " ") == c


def test_verify_code_accepts_only_the_right_code_and_never_a_missing_hash():
    h = asyncio.run(reg.hash_code("ABCD-EFGH"))
    assert h.startswith("$2") and "ABCDEFGH" not in h
    assert asyncio.run(reg.verify_code("abcd efgh", h)) is True
    assert asyncio.run(reg.verify_code("ABCDEFGJ", h)) is False
    assert asyncio.run(reg.verify_code("ABCDEFGH", None)) is False
    assert asyncio.run(reg.verify_code("", None)) is False


def _ok(**kw):
    base = dict(registration_code="ABCD-EFGH", business_name="Stall", category="FOOD", owner_full_name="Own Er",
                owner_email="o@example.com", password="longenough1", accept_terms=True)
    base.update(kw)
    return RegisterRequest(**base)


def test_register_request_policy():
    assert _ok().category == "FOOD"
    for bad in (dict(password="short"), dict(password="é" * 40), dict(accept_terms=False),
                dict(category="OTHER"), dict(owner_email="nope")):
        with pytest.raises(ValidationError):
            _ok(**bad)
