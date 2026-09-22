"""
South African mobile number normalisation — backend/app/utils/phone.py.

Pure function, no DB/network — a full table of valid/invalid shapes per
the task's own §5 acceptance criteria: common valid formats, invalid
lengths, non-SA-looking (landline) numbers, garbage input, and whitespace/
punctuation tolerance.
"""

import pytest

from backend.app.utils.phone import normalize_sa_mobile


class TestValidShapes:
    @pytest.mark.parametrize(
        "raw",
        [
            "0821234567",
            "082 123 4567",
            "+27821234567",
            "27821234567",
            "082-123-4567",
            "(082) 123 4567",
            "  0821234567  ",
            "0712345678",
            "0601234567",
        ],
    )
    def test_normalises_to_canonical_plus27(self, raw):
        assert normalize_sa_mobile(raw) == _expected(raw)


def _expected(raw: str) -> str:
    # Independent, deliberately different derivation of the expected value
    # (strip everything non-digit, take the last 9 digits, prefix +27) so
    # this test doesn't just re-implement normalize_sa_mobile's own logic.
    digits = "".join(c for c in raw if c.isdigit())
    return f"+27{digits[-9:]}"


class TestInvalidInputs:
    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "not a phone number",
            "0211234567",     # landline (021 area code) — second digit not 6/7/8
            "08212345",       # too short
            "082123456789",   # too long
            "+2782123456",    # too short with +
            "+278212345678",  # too long with +
            "27821234567890", # way too long
            "+1 555 123 4567",  # not SA at all
            "0",
            "++27821234567",
            "27021234567",    # international but landline-shaped national part
        ],
    )
    def test_returns_none(self, raw):
        assert normalize_sa_mobile(raw) is None


class TestWhitespaceAndPunctuationTolerance:
    def test_mixed_separators_all_normalise_the_same(self):
        variants = [
            "0821234567",
            "082 123 4567",
            "082-123-4567",
            "(082) 123-4567",
            " 082 123 4567 ",
        ]
        results = {normalize_sa_mobile(v) for v in variants}
        assert results == {"+27821234567"}


class TestNeverRaises:
    @pytest.mark.parametrize("raw", [123, [], {}, object()])
    def test_non_string_input_returns_none_not_raises(self, raw):
        assert normalize_sa_mobile(raw) is None
