"""
AI Email Outreach — spreadsheet import service.

Pure parsing/normalisation/validation logic plus the two DB-touching entry
points used by admin_api/app/api/v1/routes_ai_emails.py:

    create_import_preview()  — parses a file, validates + de-dupes every row,
                                persists an AiEmailImportBatch (status="previewed")
    apply_confirm()           — reads a previewed batch back and commits leads

Nothing here ever invents missing data — unparseable phones/emails are left
None with a warning/error rather than guessed.
"""

import csv
import io
import re
import uuid
from datetime import date, datetime, timezone
from typing import Literal

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from shared.models.ai_emails import AiEmailImportBatch, AiEmailLead

# ── Canonical lead fields this importer can populate ─────────────────────────

CANONICAL_FIELDS = [
    "business_name", "category", "city", "suburb", "address", "phone",
    "whatsapp", "email", "website", "source_url", "preferred_contact_method",
    "verification_status", "research_notes", "outreach_status_hint",
    "last_contacted_date", "next_follow_up_date",
]

# Alias substrings (normalised: lowercased, non-alphanumeric collapsed to
# single spaces) used to auto-suggest a column mapping from spreadsheet
# headers. Order within each list matters only for readability.
SOURCE_HEADER_ALIASES: dict[str, list[str]] = {
    "business_name": ["business name", "name", "company", "gym name", "business"],
    "category": ["category", "type", "business type"],
    "city": ["city", "region", "city region", "town"],
    "suburb": ["suburb", "area", "suburb area", "district"],
    "address": ["street address", "address"],
    "phone": ["phone", "tel", "telephone", "phone tel", "contact number", "cell"],
    "whatsapp": ["whatsapp"],
    "email": ["email", "e mail", "email address"],
    "website": ["website", "social", "website social", "website url"],
    "source_url": ["source url", "source"],
    "preferred_contact_method": ["preferred contact", "preferred contact method", "contact method"],
    "verification_status": ["verification level", "verification status", "verification"],
    "research_notes": ["research notes", "response notes", "notes"],
    "outreach_status_hint": ["outreach status", "status"],
    "last_contacted_date": ["last contact", "last contacted"],
    "next_follow_up_date": ["follow up date", "next follow up", "followup date"],
}

DuplicateStrategy = Literal["skip", "update", "create_anyway"]


def _normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", h.strip().lower()).strip()


def suggest_column_mapping(headers: list[str]) -> dict[str, str | None]:
    """
    Best-effort canonical field -> source header mapping. A header is only
    ever assigned to one canonical field (first match wins), so two fields
    never silently collide onto the same column.
    """
    normalized = {h: _normalize_header(h) for h in headers}
    used_headers: set[str] = set()
    mapping: dict[str, str | None] = {}

    for field in CANONICAL_FIELDS:
        match: str | None = None
        for alias in SOURCE_HEADER_ALIASES[field]:
            for header, norm in normalized.items():
                if header in used_headers:
                    continue
                if norm == alias or alias in norm.split() or norm.startswith(alias):
                    match = header
                    break
            if match:
                break
        mapping[field] = match
        if match:
            used_headers.add(match)

    return mapping


# ── File parsing ──────────────────────────────────────────────────────────────

def parse_spreadsheet(content: bytes, filename: str) -> tuple[list[str], list[dict]]:
    """
    Returns (headers, rows) where each row is a dict[header] = raw string value.
    Raises AppError("UNSUPPORTED_FILE_TYPE", ...) for anything but .xlsx/.csv.
    """
    lower_name = filename.lower()

    if lower_name.endswith(".csv"):
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        rows = [dict(row) for row in reader]
        return headers, rows

    if lower_name.endswith(".xlsx"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = wb.worksheets[0]
        row_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(row_iter)
        except StopIteration:
            return [], []
        headers = [str(h).strip() if h is not None else "" for h in header_row]
        rows = []
        for raw_row in row_iter:
            if raw_row is None or all(v is None for v in raw_row):
                continue
            row = {
                headers[i]: ("" if raw_row[i] is None else str(raw_row[i]))
                for i in range(min(len(headers), len(raw_row)))
            }
            rows.append(row)
        return headers, rows

    raise AppError(
        "UNSUPPORTED_FILE_TYPE",
        f"Unsupported file type for '{filename}' — only .xlsx and .csv are accepted",
        400,
    )


# ── Field normalisation ───────────────────────────────────────────────────────

def normalize_sa_phone(raw: str | None) -> str | None:
    """
    Normalises South African numbers to +27XXXXXXXXX.
    Never invents digits — returns None if the input can't be confidently
    normalised (caller surfaces this as a warning, not a fatal error).
    """
    if not raw or not raw.strip():
        return None
    digits = re.sub(r"[^\d+]", "", raw.strip())

    if digits.startswith("+27"):
        rest = digits[3:]
        return f"+27{rest}" if rest.isdigit() and len(rest) == 9 else None
    if digits.startswith("0") and digits.isdigit() and len(digits) == 10:
        return f"+27{digits[1:]}"
    if digits.startswith("27") and digits.isdigit() and len(digits) == 11:
        return f"+27{digits[2:]}"
    return None


def validate_email_value(raw: str | None) -> tuple[str | None, str | None]:
    """Returns (normalized_lowercase_email, error_message). Empty input is not an error."""
    if not raw or not raw.strip():
        return None, None
    try:
        result = validate_email(raw.strip(), check_deliverability=False)
        return result.normalized.lower(), None
    except EmailNotValidError as exc:
        return None, str(exc)


def _parse_loose_date(raw: str | None) -> date | None:
    if not raw or not raw.strip():
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


_CONTACT_METHOD_MAP = {"email": "email", "whatsapp": "whatsapp", "phone": "phone", "tel": "phone"}
_VERIFICATION_MAP = {"verified": "verified", "invalid": "invalid", "unverified": "unverified"}


def is_status_like_verify_value(value: str | None) -> bool:
    """
    True for spreadsheet status text like "Verify", "Research", "Verify email",
    "Verify phone" — rows like this must never default to a "ready" lead_status.
    """
    if not value:
        return False
    v = value.strip().lower()
    return "verify" in v or "research" in v


def compute_initial_lead_status(outreach_hint: str | None, email: str | None) -> str:
    if outreach_hint:
        v = outreach_hint.strip().lower()
        if "research" in v:
            return "requires_research"
        if "verify" in v:
            return "requires_verification"
    if not email:
        return "requires_research"
    return "new"


# ── Row-level preview building ────────────────────────────────────────────────

def build_row_preview(raw_row: dict, mapping: dict[str, str | None], row_number: int) -> dict:
    """
    Applies the column mapping to one raw spreadsheet row, normalises phone/
    email, and returns a preview row dict. Never silently drops a row — a
    fatal error still comes back with status="invalid" and explicit errors.
    """
    def get(field: str) -> str | None:
        header = mapping.get(field)
        if not header:
            return None
        val = raw_row.get(header)
        return val.strip() if isinstance(val, str) else val

    errors: list[str] = []
    warnings: list[str] = []

    business_name = get("business_name")
    if not business_name:
        errors.append("business_name: required field is missing")

    phone_raw = get("phone")
    phone = normalize_sa_phone(phone_raw)
    if phone_raw and not phone:
        warnings.append(f"phone: could not normalise '{phone_raw}' — left blank")

    whatsapp_raw = get("whatsapp")
    whatsapp = normalize_sa_phone(whatsapp_raw)
    if whatsapp_raw and not whatsapp:
        warnings.append(f"whatsapp: could not normalise '{whatsapp_raw}' — left blank")

    email_raw = get("email")
    email, email_error = validate_email_value(email_raw)
    if email_error:
        errors.append(f"email: {email_error}")

    contact_method_raw = (get("preferred_contact_method") or "").strip().lower()
    preferred_contact_method = _CONTACT_METHOD_MAP.get(contact_method_raw, "unknown")

    verification_raw = (get("verification_status") or "").strip().lower()
    verification_status = _VERIFICATION_MAP.get(verification_raw, "unverified")

    outreach_hint = get("outreach_status_hint")
    lead_status = compute_initial_lead_status(outreach_hint, email)

    research_notes = get("research_notes")
    if outreach_hint and is_status_like_verify_value(outreach_hint):
        note = f"Imported outreach status: {outreach_hint}"
        research_notes = f"{research_notes}\n{note}" if research_notes else note

    data = {
        "business_name": business_name,
        "category": get("category"),
        "city": get("city"),
        "suburb": get("suburb"),
        "address": get("address"),
        "phone": phone,
        "whatsapp": whatsapp,
        "email": email,
        "website": get("website"),
        "source_url": get("source_url"),
        "preferred_contact_method": preferred_contact_method,
        "verification_status": verification_status,
        "lead_status": lead_status,
        "research_notes": research_notes,
        "last_contacted_date": _parse_loose_date(get("last_contacted_date")).isoformat()
            if _parse_loose_date(get("last_contacted_date")) else None,
        "next_follow_up_date": _parse_loose_date(get("next_follow_up_date")).isoformat()
            if _parse_loose_date(get("next_follow_up_date")) else None,
    }

    return {
        "row_number": row_number,
        "status": "invalid" if errors else "valid",
        "data": data,
        "errors": errors,
        "warnings": warnings,
        "duplicate_reason": None,
        "existing_lead_id": None,
    }


# ── Duplicate detection ───────────────────────────────────────────────────────

async def find_duplicates(db: AsyncSession, preview_rows: list[dict]) -> None:
    """
    Mutates preview_rows in place: valid rows that match an existing lead (by
    exact email, or by business_name+city) or another row earlier in the same
    file get status="duplicate" and a duplicate_reason. Rows already marked
    "invalid" are left alone.
    """
    seen_emails: dict[str, int] = {}
    seen_name_city: dict[tuple[str, str], int] = {}

    for row in preview_rows:
        if row["status"] != "valid":
            continue
        data = row["data"]
        email = (data.get("email") or "").lower()
        name_city = ((data.get("business_name") or "").lower(), (data.get("city") or "").lower())

        if email and email in seen_emails:
            row["status"] = "duplicate"
            row["duplicate_reason"] = "duplicate_in_batch"
            continue
        if name_city[0] and name_city in seen_name_city:
            row["status"] = "duplicate"
            row["duplicate_reason"] = "duplicate_in_batch"
            continue

        existing_id = None
        reason = None
        if email:
            result = await db.execute(
                select(AiEmailLead.id).where(func.lower(AiEmailLead.email) == email)
            )
            existing_id = result.scalar_one_or_none()
            if existing_id:
                reason = "duplicate_email"
        if not existing_id and name_city[0] and name_city[1]:
            result = await db.execute(
                select(AiEmailLead.id).where(
                    func.lower(AiEmailLead.business_name) == name_city[0],
                    func.lower(AiEmailLead.city) == name_city[1],
                )
            )
            existing_id = result.scalar_one_or_none()
            if existing_id:
                reason = "duplicate_name_city"

        if existing_id:
            row["status"] = "duplicate"
            row["duplicate_reason"] = reason
            row["existing_lead_id"] = str(existing_id)
        else:
            if email:
                seen_emails[email] = row["row_number"]
            if name_city[0]:
                seen_name_city[name_city] = row["row_number"]


# ── Preview orchestration ─────────────────────────────────────────────────────

async def create_import_preview(
    db: AsyncSession,
    admin_user_id: uuid.UUID,
    content: bytes,
    filename: str,
) -> AiEmailImportBatch:
    headers, raw_rows = parse_spreadsheet(content, filename)
    mapping = suggest_column_mapping(headers)

    preview_rows = [
        build_row_preview(raw_row, mapping, row_number=i + 2)  # +2: header is row 1
        for i, raw_row in enumerate(raw_rows)
    ]
    await find_duplicates(db, preview_rows)

    valid = sum(1 for r in preview_rows if r["status"] == "valid")
    duplicate = sum(1 for r in preview_rows if r["status"] == "duplicate")
    invalid = sum(1 for r in preview_rows if r["status"] == "invalid")

    batch = AiEmailImportBatch(
        filename=filename,
        file_type="xlsx" if filename.lower().endswith(".xlsx") else "csv",
        status="previewed",
        uploaded_by_admin_user_id=admin_user_id,
        column_mapping_json=mapping,
        preview_rows_json=preview_rows,
        total_rows=len(preview_rows),
        valid_rows=valid,
        duplicate_rows=duplicate,
        rejected_rows=invalid,
    )
    db.add(batch)
    await db.flush()
    return batch


# ── Confirm ───────────────────────────────────────────────────────────────────

async def apply_confirm(
    db: AsyncSession,
    batch: AiEmailImportBatch,
    skip_row_numbers: set[int],
    duplicate_strategy: DuplicateStrategy,
) -> dict:
    created_count = 0
    updated_count = 0
    skipped_count = 0
    created_lead_ids: list[str] = []

    for row in batch.preview_rows_json:
        row_number = row["row_number"]
        if row_number in skip_row_numbers or row["status"] == "invalid":
            skipped_count += 1
            continue

        data = row["data"]
        if row["status"] == "duplicate":
            if duplicate_strategy == "skip":
                skipped_count += 1
                continue
            if duplicate_strategy == "update" and row.get("existing_lead_id"):
                result = await db.execute(
                    select(AiEmailLead).where(AiEmailLead.id == uuid.UUID(row["existing_lead_id"]))
                )
                lead = result.scalar_one_or_none()
                if lead is None:
                    skipped_count += 1
                    continue
                for field in (
                    "category", "city", "suburb", "address", "phone", "whatsapp",
                    "email", "website", "source_url", "preferred_contact_method",
                    "verification_status", "research_notes",
                ):
                    value = data.get(field)
                    if value:
                        setattr(lead, field, value)
                lead.import_batch_id = batch.id
                updated_count += 1
                continue
            # "create_anyway", or "update" with no existing_lead_id (in-batch
            # duplicate — nothing to update against yet): fall through to create.

        lead = AiEmailLead(
            business_name=data["business_name"],
            category=data.get("category"),
            city=data.get("city"),
            suburb=data.get("suburb"),
            address=data.get("address"),
            phone=data.get("phone"),
            whatsapp=data.get("whatsapp"),
            email=data.get("email"),
            website=data.get("website"),
            source_url=data.get("source_url"),
            preferred_contact_method=data.get("preferred_contact_method", "unknown"),
            verification_status=data.get("verification_status", "unverified"),
            lead_status=data.get("lead_status", "new"),
            research_notes=data.get("research_notes"),
            import_batch_id=batch.id,
        )
        db.add(lead)
        await db.flush()
        created_count += 1
        created_lead_ids.append(str(lead.id))

    batch.status = "confirmed"
    batch.created_count = created_count
    batch.updated_count = updated_count
    batch.skipped_count = skipped_count
    batch.confirmed_at = datetime.now(timezone.utc)

    return {
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "created_lead_ids": created_lead_ids,
    }
