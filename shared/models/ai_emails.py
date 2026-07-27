"""
AI Email Outreach — lead, import batch, and case-study profile models.

Super-admin-only lead-generation/outreach module (see "AI emails/" docs at the
repo root) for NextGen Intelligence's custom-software sales campaign — NOT a
feature of, or variant of, the WhatsApp ordering product also hosted in this
repo. Leads are external sales prospects (gyms/sports facilities) sourced from
spreadsheet imports. This module has zero foreign keys, joins, or code
dependency on `businesses` or any of its related tables — that must never
change. No relationship() to AdminUser is declared here, matching how
shared/models/audit.py::AuditEvent keeps its actor FK as a bare column rather
than an ORM relationship.

Phase 1 (+ case-study profile, added ahead of Phase 2 as compliance/data
infrastructure): import batches, leads, case studies. No campaigns/generated-
emails/sent-emails/replies/followups/gmail-connections tables yet — those come
in later phases.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AiEmailImportBatch(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "ai_email_import_batches"

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(
        String(8), nullable=False,
        comment="xlsx or csv"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="previewed", nullable=False,
        comment="previewed / confirmed / failed / expired"
    )
    uploaded_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    column_mapping_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False,
        comment='Final {target_field: source_header} mapping used for this import'
    )
    preview_rows_json: Mapped[list] = mapped_column(
        JSONB, nullable=False,
        comment="Parsed+validated rows produced at preview time; read back by /import/confirm"
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(__import__("datetime").timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AiEmailImportBatch {self.filename} ({self.status})>"


class AiEmailLead(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "ai_email_leads"

    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    suburb: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(
        String(20), nullable=True,
        comment="Normalized +27XXXXXXXXX; NULL if unparseable — never invented"
    )
    whatsapp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="Validated + lowercased at import time"
    )
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    preferred_contact_method: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False,
        comment="email / whatsapp / phone / unknown"
    )
    verification_status: Mapped[str] = mapped_column(
        String(16), default="unverified", nullable=False,
        comment="unverified / verified / invalid — gates auto-send in later phases"
    )
    lead_status: Mapped[str] = mapped_column(
        String(24), default="new", nullable=False,
        comment=(
            "Full pipeline vocabulary (see DB CHECK constraint / DATABASE_SCHEMA.md). "
            "Phase 1 only ever writes: new, requires_research, requires_verification, "
            "ready_to_generate."
        )
    )

    research_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_research_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Unused until Phase 2+"
    )
    assigned_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, nullable=False
    )
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    phone_outreach_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Manual tick for leads without an email — worked by phone/WhatsApp instead"
    )
    phone_outreach_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unsubscribed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unsubscribe_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_contacted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_follow_up_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_email_import_batches.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<AiEmailLead {self.business_name} ({self.lead_status})>"


class AiEmailCaseStudy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Structured, approved facts for one case-study reference project (e.g.
    Muscle Factory), used by the Phase 2 email generator instead of embedding
    facts only in prompt text. The generator must never state a fact that
    isn't present here, must refuse to use a case study whose
    approved_for_marketing is false, and must not name the client unless
    client_permission_status = 'granted'.
    """
    __tablename__ = "ai_email_case_studies"

    case_study_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)

    approved_for_marketing: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="NextGen's internal editorial approval of this content for outbound use"
    )
    confirmed_deployed_features: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False,
        comment="Facts that may be stated as already built — list of strings"
    )
    optional_or_proposed_features: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False,
        comment="Facts that must only be phrased as can-be-added/configured — list of strings"
    )
    approved_credibility_statement: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Vetted paragraph the generator may paraphrase — never invent beyond this"
    )
    approved_screenshots: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False,
        comment="[{label, url, redacted}] — empty until screenshots exist and are cleared"
    )
    screenshot_redaction_status: Mapped[str] = mapped_column(
        String(16), default="not_applicable", nullable=False,
        comment="not_applicable / pending / completed"
    )
    client_permission_status: Mapped[str] = mapped_column(
        String(16), default="not_requested", nullable=False,
        comment="not_requested / requested / granted / denied — whether the CLIENT consented "
                "to being named/referenced with third parties; separate from approved_for_marketing"
    )
    last_reviewed_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    def __repr__(self) -> str:
        return f"<AiEmailCaseStudy {self.case_study_name} (approved={self.approved_for_marketing})>"
