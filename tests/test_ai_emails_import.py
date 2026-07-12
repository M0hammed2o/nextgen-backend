"""
Tests for admin_api/app/services/ai_emails_import.py — spreadsheet parsing,
column mapping, phone/email normalisation, and duplicate detection for the
AI Email Outreach lead importer. Pure-function tests; duplicate detection
uses a mocked AsyncSession (no live database), matching this test suite's
existing no-DB-required unit-test style.
"""

import csv
import io
import uuid
from unittest.mock import AsyncMock, MagicMock

import openpyxl
import pytest

from admin_api.app.services.ai_emails_import import (
    build_row_preview,
    compute_initial_lead_status,
    find_duplicates,
    is_status_like_verify_value,
    normalize_sa_phone,
    parse_spreadsheet,
    suggest_column_mapping,
    validate_email_value,
)


# ── normalize_sa_phone ────────────────────────────────────────────────────────

class TestNormalizeSaPhone:
    def test_local_format(self):
        assert normalize_sa_phone("0831234567") == "+27831234567"

    def test_local_format_with_spaces(self):
        assert normalize_sa_phone("083 123 4567") == "+27831234567"

    def test_local_format_with_dashes(self):
        assert normalize_sa_phone("083-123-4567") == "+27831234567"

    def test_already_international(self):
        assert normalize_sa_phone("+27831234567") == "+27831234567"

    def test_international_without_plus(self):
        assert normalize_sa_phone("27831234567") == "+27831234567"

    def test_garbage_returns_none(self):
        assert normalize_sa_phone("abc") is None

    def test_too_short_returns_none(self):
        assert normalize_sa_phone("12345") is None

    def test_empty_returns_none(self):
        assert normalize_sa_phone("") is None
        assert normalize_sa_phone(None) is None


# ── validate_email_value ──────────────────────────────────────────────────────

class TestValidateEmailValue:
    def test_valid_email_lowercased(self):
        email, error = validate_email_value("Info@MuscleFactory.co.za")
        assert email == "info@musclefactory.co.za"
        assert error is None

    def test_invalid_email_returns_error(self):
        email, error = validate_email_value("not-an-email")
        assert email is None
        assert error is not None

    def test_empty_is_not_an_error(self):
        email, error = validate_email_value("")
        assert email is None
        assert error is None


# ── is_status_like_verify_value / compute_initial_lead_status ────────────────

class TestVerifyStatusDetection:
    @pytest.mark.parametrize("value", ["Verify", "Research", "Verify email", "Verify phone", "PLEASE VERIFY"])
    def test_verify_like_values_detected(self, value):
        assert is_status_like_verify_value(value) is True

    def test_non_verify_value_not_detected(self):
        assert is_status_like_verify_value("Contacted") is False

    def test_research_hint_maps_to_requires_research(self):
        assert compute_initial_lead_status("Research", "a@b.com") == "requires_research"

    def test_verify_email_hint_maps_to_requires_verification(self):
        assert compute_initial_lead_status("Verify email", "a@b.com") == "requires_verification"

    def test_no_hint_no_email_requires_research(self):
        assert compute_initial_lead_status(None, None) == "requires_research"

    def test_no_hint_with_email_is_new(self):
        assert compute_initial_lead_status(None, "a@b.com") == "new"

    def test_verify_status_never_yields_ready_status(self):
        for hint in ["Verify", "Research", "Verify email", "Verify phone"]:
            status = compute_initial_lead_status(hint, "a@b.com")
            assert status in ("requires_research", "requires_verification")


# ── suggest_column_mapping ────────────────────────────────────────────────────

class TestSuggestColumnMapping:
    def test_maps_common_headers(self):
        headers = ["Business Name", "City / Region", "Suburb / Area", "Phone / Tel", "Email"]
        mapping = suggest_column_mapping(headers)
        assert mapping["business_name"] == "Business Name"
        assert mapping["city"] == "City / Region"
        assert mapping["suburb"] == "Suburb / Area"
        assert mapping["phone"] == "Phone / Tel"
        assert mapping["email"] == "Email"

    def test_unmatched_field_is_none(self):
        mapping = suggest_column_mapping(["Business Name"])
        assert mapping["whatsapp"] is None

    def test_header_not_double_mapped(self):
        # "Email" should only ever satisfy one canonical field.
        mapping = suggest_column_mapping(["Email"])
        used = [v for v in mapping.values() if v == "Email"]
        assert len(used) == 1


# ── parse_spreadsheet ──────────────────────────────────────────────────────────

class TestParseSpreadsheet:
    def test_csv_parsing(self):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Business Name", "City", "Email"])
        writer.writerow(["Durban Fitness Club", "Durban", "info@durbanfitness.co.za"])
        content = buf.getvalue().encode("utf-8")

        headers, rows = parse_spreadsheet(content, "leads.csv")
        assert headers == ["Business Name", "City", "Email"]
        assert len(rows) == 1
        assert rows[0]["Business Name"] == "Durban Fitness Club"

    def test_csv_with_bom(self):
        content = "Business Name,City\nPMB Gym,Pietermaritzburg\n".encode("utf-8-sig")
        headers, rows = parse_spreadsheet(content, "leads.csv")
        assert headers[0] == "Business Name"
        assert rows[0]["Business Name"] == "PMB Gym"

    def test_xlsx_parsing(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Business Name", "City", "Email"])
        ws.append(["Durban Fitness Club", "Durban", "info@durbanfitness.co.za"])
        buf = io.BytesIO()
        wb.save(buf)
        content = buf.getvalue()

        headers, rows = parse_spreadsheet(content, "leads.xlsx")
        assert headers == ["Business Name", "City", "Email"]
        assert len(rows) == 1
        assert rows[0]["Business Name"] == "Durban Fitness Club"

    def test_csv_and_xlsx_equivalent(self):
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["Business Name", "City"])
        w.writerow(["Test Gym", "Durban"])
        csv_headers, csv_rows = parse_spreadsheet(csv_buf.getvalue().encode("utf-8"), "a.csv")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Business Name", "City"])
        ws.append(["Test Gym", "Durban"])
        xlsx_buf = io.BytesIO()
        wb.save(xlsx_buf)
        xlsx_headers, xlsx_rows = parse_spreadsheet(xlsx_buf.getvalue(), "a.xlsx")

        assert csv_headers == xlsx_headers
        assert csv_rows[0]["Business Name"] == xlsx_rows[0]["Business Name"]

    def test_unsupported_extension_raises(self):
        from backend.app.core.errors import AppError
        with pytest.raises(AppError):
            parse_spreadsheet(b"whatever", "leads.txt")


# ── build_row_preview ─────────────────────────────────────────────────────────

class TestBuildRowPreview:
    def test_missing_business_name_is_invalid(self):
        mapping = {"business_name": "Business Name", "email": "Email"}
        row = build_row_preview({"Business Name": "", "Email": "a@b.com"}, mapping, row_number=2)
        assert row["status"] == "invalid"
        assert any("business_name" in e for e in row["errors"])

    def test_invalid_email_is_invalid(self):
        mapping = {"business_name": "Business Name", "email": "Email"}
        row = build_row_preview(
            {"Business Name": "Test Gym", "Email": "not-an-email"}, mapping, row_number=2
        )
        assert row["status"] == "invalid"
        assert any("email" in e for e in row["errors"])

    def test_valid_row(self):
        mapping = {"business_name": "Business Name", "email": "Email", "phone": "Phone"}
        row = build_row_preview(
            {"Business Name": "Test Gym", "Email": "info@test.co.za", "Phone": "0831234567"},
            mapping,
            row_number=2,
        )
        assert row["status"] == "valid"
        assert row["data"]["email"] == "info@test.co.za"
        assert row["data"]["phone"] == "+27831234567"

    def test_unparseable_phone_is_warning_not_error(self):
        mapping = {"business_name": "Business Name", "phone": "Phone"}
        row = build_row_preview({"Business Name": "Test Gym", "Phone": "abc"}, mapping, row_number=2)
        assert row["status"] == "valid"
        assert row["data"]["phone"] is None
        assert any("phone" in w for w in row["warnings"])


# ── find_duplicates (mocked AsyncSession — no live DB) ────────────────────────

class TestFindDuplicates:
    @pytest.mark.asyncio
    async def test_in_batch_duplicate_email(self):
        db = AsyncMock()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        db.execute.return_value = exec_result

        rows = [
            {"row_number": 2, "status": "valid", "data": {"business_name": "Gym A", "email": "a@b.com", "city": "Durban"}, "duplicate_reason": None, "existing_lead_id": None},
            {"row_number": 3, "status": "valid", "data": {"business_name": "Gym B", "email": "a@b.com", "city": "Durban"}, "duplicate_reason": None, "existing_lead_id": None},
        ]
        await find_duplicates(db, rows)

        assert rows[0]["status"] == "valid"
        assert rows[1]["status"] == "duplicate"
        assert rows[1]["duplicate_reason"] == "duplicate_in_batch"

    @pytest.mark.asyncio
    async def test_existing_db_email_duplicate(self):
        db = AsyncMock()
        existing_id = uuid.uuid4()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = existing_id
        db.execute.return_value = exec_result

        rows = [
            {"row_number": 2, "status": "valid", "data": {"business_name": "Gym A", "email": "a@b.com", "city": "Durban"}, "duplicate_reason": None, "existing_lead_id": None},
        ]
        await find_duplicates(db, rows)

        assert rows[0]["status"] == "duplicate"
        assert rows[0]["duplicate_reason"] == "duplicate_email"
        assert rows[0]["existing_lead_id"] == str(existing_id)

    @pytest.mark.asyncio
    async def test_invalid_rows_are_skipped(self):
        db = AsyncMock()
        rows = [
            {"row_number": 2, "status": "invalid", "data": {"business_name": "", "email": None, "city": None}, "duplicate_reason": None, "existing_lead_id": None},
        ]
        await find_duplicates(db, rows)
        assert rows[0]["status"] == "invalid"
        db.execute.assert_not_called()
