"""
Tests for _parse_name_and_phone — the fix for Issue 3.

The bug: entire "Mohammed Moosa\n0837866021" was stored as the customer name
because the old code did `text = msg_text.strip()` then immediately stored
the whole string.

The fix splits on newlines, detects which part is a phone (9-15 digits after
stripping non-digit/+ chars), and stores them separately.
"""

import pytest

from backend.app.bot.pipeline import _parse_name_and_phone


class TestParseNameAndPhone:

    # ── Phone-only messages ───────────────────────────────────────────────────

    def test_phone_only_local(self):
        name, phone = _parse_name_and_phone("0837866021")
        assert name is None
        assert phone == "0837866021"

    def test_phone_only_with_country_code(self):
        name, phone = _parse_name_and_phone("+27837866021")
        assert name is None
        assert phone == "+27837866021"

    def test_phone_only_with_spaces(self):
        name, phone = _parse_name_and_phone("083 786 6021")
        # Spaces stripped before digit-count check
        assert name is None
        assert phone == "0837866021"

    def test_phone_only_with_dashes(self):
        name, phone = _parse_name_and_phone("083-786-6021")
        assert name is None
        assert phone == "0837866021"

    # ── Name-only messages ────────────────────────────────────────────────────

    def test_name_only_single_word(self):
        name, phone = _parse_name_and_phone("Mohammed")
        assert name == "Mohammed"
        assert phone is None

    def test_name_only_full_name(self):
        name, phone = _parse_name_and_phone("Mohammed Moosa")
        assert name == "Mohammed Moosa"
        assert phone is None

    def test_name_only_with_whitespace(self):
        name, phone = _parse_name_and_phone("  Mohammed Moosa  ")
        assert name == "Mohammed Moosa"
        assert phone is None

    # ── Multi-line: name then phone ───────────────────────────────────────────

    def test_name_then_phone_newline(self):
        name, phone = _parse_name_and_phone("Mohammed Moosa\n0837866021")
        assert name == "Mohammed Moosa"
        assert phone == "0837866021"

    def test_name_then_phone_with_country_code(self):
        name, phone = _parse_name_and_phone("Mohammed Moosa\n+27837866021")
        assert name == "Mohammed Moosa"
        assert phone == "+27837866021"

    def test_name_then_phone_with_spaces_in_phone(self):
        name, phone = _parse_name_and_phone("Mohammed Moosa\n083 786 6021")
        assert name == "Mohammed Moosa"
        assert phone == "0837866021"

    # ── Multi-line: phone then name ───────────────────────────────────────────

    def test_phone_then_name_newline(self):
        """Phone number on first line, name on second — must still parse correctly."""
        name, phone = _parse_name_and_phone("0837866021\nMohammed Moosa")
        assert name == "Mohammed Moosa"
        assert phone == "0837866021"

    def test_phone_with_country_code_then_name(self):
        name, phone = _parse_name_and_phone("+27837866021\nMohammed Moosa")
        assert name == "Mohammed Moosa"
        assert phone == "+27837866021"

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_string(self):
        name, phone = _parse_name_and_phone("")
        assert name is None
        assert phone is None

    def test_whitespace_only(self):
        name, phone = _parse_name_and_phone("   \n  ")
        assert name is None
        assert phone is None

    def test_three_lines_name_phone_city(self):
        """Extra lines beyond name+phone should merge into name."""
        name, phone = _parse_name_and_phone("Mohammed Moosa\n0837866021\nCape Town")
        # Phone extracted, name parts = "Mohammed Moosa" + "Cape Town"
        assert phone == "0837866021"
        assert name is not None
        assert "Mohammed Moosa" in name

    def test_short_number_treated_as_name(self):
        """8 digits is below the 9-digit threshold — treated as name, not phone."""
        name, phone = _parse_name_and_phone("12345678")
        assert name == "12345678"
        assert phone is None

    def test_only_one_phone_extracted_even_if_two_present(self):
        """Only first phone-like line is captured."""
        name, phone = _parse_name_and_phone("0837866021\n0721234567")
        # First is phone, second exceeds digit threshold too — treated as second name part
        assert phone == "0837866021"

    def test_name_with_numbers_in_it(self):
        """Names like 'Room 101' should not be mistaken for a phone number."""
        # "101" is only 3 digits — below threshold
        name, phone = _parse_name_and_phone("Room 101")
        assert name == "Room 101"
        assert phone is None
