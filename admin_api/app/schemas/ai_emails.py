"""
AI Email Outreach — Pydantic request/response schemas.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


# ── Import preview / confirm ─────────────────────────────────────────────────

class ImportPreviewRow(BaseModel):
    row_number: int
    status: Literal["valid", "invalid", "duplicate"]
    data: dict
    errors: list[str] = []
    warnings: list[str] = []
    duplicate_reason: str | None = None
    existing_lead_id: str | None = None


class ImportPreviewResponse(BaseModel):
    batch_id: uuid.UUID
    filename: str
    file_type: str
    detected_headers: list[str]
    suggested_mapping: dict[str, str | None]
    total_rows: int
    valid_rows: int
    duplicate_rows: int
    rejected_rows: int
    preview_rows: list[ImportPreviewRow]


class ImportConfirmRequest(BaseModel):
    batch_id: uuid.UUID
    skip_row_numbers: list[int] = Field(default_factory=list)
    duplicate_strategy: Literal["skip", "update", "create_anyway"] = "skip"


class ImportConfirmResponse(BaseModel):
    batch_id: uuid.UUID
    status: str
    created_count: int
    updated_count: int
    skipped_count: int
    created_lead_ids: list[str]


# ── Leads ─────────────────────────────────────────────────────────────────────

class LeadResponse(BaseModel):
    id: uuid.UUID
    business_name: str
    category: str | None
    city: str | None
    suburb: str | None
    phone: str | None
    whatsapp: str | None
    email: str | None
    website: str | None
    preferred_contact_method: str
    verification_status: str
    lead_status: str
    do_not_contact: bool
    assigned_admin_user_id: uuid.UUID | None
    last_contacted_date: date | None
    next_follow_up_date: date | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class LeadDetailResponse(LeadResponse):
    address: str | None
    source_url: str | None
    research_notes: str | None
    ai_research_summary: str | None
    tags: list[str]
    unsubscribed_at: datetime | None
    unsubscribe_reason: str | None
    import_batch_id: uuid.UUID | None
