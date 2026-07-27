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
    phone_outreach_completed: bool
    phone_outreach_completed_at: datetime | None
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


class LeadUpdateRequest(BaseModel):
    """Phase 1 only supports toggling the manual phone-outreach tick."""
    phone_outreach_completed: bool


# ── Case studies ──────────────────────────────────────────────────────────────

class CaseStudyResponse(BaseModel):
    id: uuid.UUID
    case_study_name: str
    client_name: str
    industry: str | None
    approved_for_marketing: bool
    confirmed_deployed_features: list[str]
    optional_or_proposed_features: list[str]
    approved_credibility_statement: str | None
    approved_screenshots: list[dict]
    screenshot_redaction_status: str
    client_permission_status: str
    last_reviewed_date: date | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class CaseStudyUpdateRequest(BaseModel):
    """
    All fields optional — PATCH semantics. Editing this record is a
    compliance action (approving marketing content, recording client
    consent), so every change is audit-logged with a before/after diff.
    """
    case_study_name: str | None = None
    client_name: str | None = None
    industry: str | None = None
    approved_for_marketing: bool | None = None
    confirmed_deployed_features: list[str] | None = None
    optional_or_proposed_features: list[str] | None = None
    approved_credibility_statement: str | None = None
    approved_screenshots: list[dict] | None = None
    screenshot_redaction_status: Literal["not_applicable", "pending", "completed"] | None = None
    client_permission_status: Literal["not_requested", "requested", "granted", "denied"] | None = None
    last_reviewed_date: date | None = None
