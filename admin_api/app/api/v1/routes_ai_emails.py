"""
AI Email Outreach — lead import + lead list/detail routes.
Only accessible by SUPER_ADMIN. Phase 1: no AI generation, no Gmail, no sending.
"""

import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.app.schemas.ai_emails import (
    CaseStudyResponse,
    CaseStudyUpdateRequest,
    ImportConfirmRequest,
    ImportConfirmResponse,
    ImportPreviewResponse,
    LeadDetailResponse,
    LeadResponse,
    LeadUpdateRequest,
)
from admin_api.app.services.ai_emails_import import apply_confirm, create_import_preview
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.pagination import OffsetParams, PaginatedResponse, PaginationMeta
from admin_api.app.core.deps import require_super_admin
from backend.app.core.rbac import AuthUser
from backend.app.db.session import get_db
from shared.models.ai_emails import AiEmailCaseStudy, AiEmailImportBatch, AiEmailLead
from shared.models.audit import AuditEvent

logger = logging.getLogger("nextgen.admin")
router = APIRouter(prefix="/admin/ai-emails", tags=["admin-ai-emails"])


# ── Import ────────────────────────────────────────────────────────────────────

@router.post("/import/preview", response_model=ImportPreviewResponse)
async def import_preview(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse an uploaded .xlsx/.csv, validate + de-dupe every row, and persist
    the parsed rows as a new import batch (status="previewed"). Does NOT
    write to ai_email_leads — that only happens on /import/confirm.
    """
    if not file.filename:
        raise AppError("MISSING_FILENAME", "Uploaded file has no filename", 400)

    content = await file.read()
    batch = await create_import_preview(
        db=db,
        admin_user_id=user.user_id,
        content=content,
        filename=file.filename,
    )
    await db.commit()

    return ImportPreviewResponse(
        batch_id=batch.id,
        filename=batch.filename,
        file_type=batch.file_type,
        detected_headers=list(batch.column_mapping_json.values()),
        suggested_mapping=batch.column_mapping_json,
        total_rows=batch.total_rows,
        valid_rows=batch.valid_rows,
        duplicate_rows=batch.duplicate_rows,
        rejected_rows=batch.rejected_rows,
        preview_rows=batch.preview_rows_json,
    )


@router.post("/import/confirm", response_model=ImportConfirmResponse)
async def import_confirm(
    body: ImportConfirmRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """Commit a previously-previewed import batch into ai_email_leads."""
    result = await db.execute(
        select(AiEmailImportBatch).where(AiEmailImportBatch.id == body.batch_id)
    )
    batch = result.scalar_one_or_none()
    if not batch:
        raise NotFoundError("ImportBatch", str(body.batch_id))
    if batch.status != "previewed":
        raise AppError(
            "BATCH_NOT_PREVIEWED",
            f"Import batch is '{batch.status}', expected 'previewed' — it may already be confirmed",
            409,
        )

    counts = await apply_confirm(
        db=db,
        batch=batch,
        skip_row_numbers=set(body.skip_row_numbers),
        duplicate_strategy=body.duplicate_strategy,
    )

    db.add(AuditEvent(
        scope="PLATFORM",
        actor_user_id=user.user_id,
        action="ai_email.import.confirmed",
        target_type="ai_email_import_batch",
        target_id=batch.id,
        diff_json=counts,
    ))
    await db.commit()

    return ImportConfirmResponse(
        batch_id=batch.id,
        status=batch.status,
        created_count=counts["created_count"],
        updated_count=counts["updated_count"],
        skipped_count=counts["skipped_count"],
        created_lead_ids=counts["created_lead_ids"],
    )


# ── Leads ─────────────────────────────────────────────────────────────────────

@router.get("/leads", response_model=PaginatedResponse)
async def list_leads(
    params: OffsetParams = Depends(),
    lead_status: str | None = Query(default=None),
    city: str | None = Query(default=None),
    search: str | None = Query(default=None),
    assigned_admin_user_id: uuid.UUID | None = Query(default=None),
    do_not_contact: bool | None = Query(default=None),
    has_email: bool | None = Query(
        default=None,
        description="true = only leads with a usable email (the send list); false = leads to work by phone/WhatsApp (the call list)",
    ),
    phone_outreach_completed: bool | None = Query(default=None),
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(AiEmailLead)
    count_query = select(func.count()).select_from(AiEmailLead)

    filters = []
    if lead_status:
        filters.append(AiEmailLead.lead_status == lead_status)
    if city:
        filters.append(func.lower(AiEmailLead.city) == city.lower())
    if assigned_admin_user_id:
        filters.append(AiEmailLead.assigned_admin_user_id == assigned_admin_user_id)
    if do_not_contact is not None:
        filters.append(AiEmailLead.do_not_contact == do_not_contact)
    if has_email is not None:
        no_email = or_(AiEmailLead.email.is_(None), AiEmailLead.email == "")
        filters.append(~no_email if has_email else no_email)
    if phone_outreach_completed is not None:
        filters.append(AiEmailLead.phone_outreach_completed == phone_outreach_completed)
    if search:
        like = f"%{search.lower()}%"
        filters.append(or_(
            func.lower(AiEmailLead.business_name).like(like),
            func.lower(AiEmailLead.email).like(like),
        ))

    for f in filters:
        query = query.where(f)
        count_query = count_query.where(f)

    total = (await db.execute(count_query)).scalar_one()

    query = query.order_by(AiEmailLead.created_at.desc()).offset(params.offset).limit(params.per_page)
    rows = (await db.execute(query)).scalars().all()

    return PaginatedResponse(
        data=[LeadResponse.model_validate(r) for r in rows],
        pagination=PaginationMeta(
            total=total,
            page=params.page,
            per_page=params.per_page,
            has_more=params.offset + len(rows) < total,
        ),
    )


@router.get("/leads/{lead_id}", response_model=LeadDetailResponse)
async def get_lead(
    lead_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AiEmailLead).where(AiEmailLead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise NotFoundError("Lead", str(lead_id))
    return LeadDetailResponse.model_validate(lead)


@router.patch("/leads/{lead_id}", response_model=LeadDetailResponse)
async def update_lead(
    lead_id: uuid.UUID,
    body: LeadUpdateRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Phase 1 only supports the manual phone-outreach tick — for leads with no
    usable email, the operator calls/WhatsApps them directly and ticks this
    once that contact is done. Not a lead_status transition; independent of
    the (not-yet-built) email pipeline.
    """
    result = await db.execute(select(AiEmailLead).where(AiEmailLead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        raise NotFoundError("Lead", str(lead_id))

    lead.phone_outreach_completed = body.phone_outreach_completed
    lead.phone_outreach_completed_at = (
        datetime.now(timezone.utc) if body.phone_outreach_completed else None
    )

    db.add(AuditEvent(
        scope="PLATFORM",
        actor_user_id=user.user_id,
        action="ai_email.lead.phone_outreach_toggled",
        target_type="ai_email_lead",
        target_id=lead.id,
        diff_json={"phone_outreach_completed": body.phone_outreach_completed},
    ))
    await db.commit()
    await db.refresh(lead)
    return LeadDetailResponse.model_validate(lead)


# ── Case studies ──────────────────────────────────────────────────────────────
# Structured, approved facts for reference projects (e.g. Muscle Factory) that
# the Phase 2 email generator must read from instead of prompt text. Editing
# these is a compliance action (marketing approval, client consent), not a
# content-editing convenience — every change is audit-logged with a diff.

@router.get("/case-studies", response_model=list[CaseStudyResponse])
async def list_case_studies(
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AiEmailCaseStudy).order_by(AiEmailCaseStudy.case_study_name))
    return [CaseStudyResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/case-studies/{case_study_id}", response_model=CaseStudyResponse)
async def get_case_study(
    case_study_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AiEmailCaseStudy).where(AiEmailCaseStudy.id == case_study_id))
    case_study = result.scalar_one_or_none()
    if not case_study:
        raise NotFoundError("CaseStudy", str(case_study_id))
    return CaseStudyResponse.model_validate(case_study)


@router.patch("/case-studies/{case_study_id}", response_model=CaseStudyResponse)
async def update_case_study(
    case_study_id: uuid.UUID,
    body: CaseStudyUpdateRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AiEmailCaseStudy).where(AiEmailCaseStudy.id == case_study_id))
    case_study = result.scalar_one_or_none()
    if not case_study:
        raise NotFoundError("CaseStudy", str(case_study_id))

    def _json_safe(value):
        # date/datetime aren't JSON-serializable by default — diff_json is
        # stored as JSONB, so isoformat them before they hit the audit log.
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    updates = body.model_dump(exclude_unset=True)
    diff: dict = {}
    for field, new_value in updates.items():
        old_value = getattr(case_study, field)
        if old_value != new_value:
            diff[field] = {"old": _json_safe(old_value), "new": _json_safe(new_value)}
            setattr(case_study, field, new_value)

    if diff:
        db.add(AuditEvent(
            scope="PLATFORM",
            actor_user_id=user.user_id,
            action="ai_email.case_study.updated",
            target_type="ai_email_case_study",
            target_id=case_study.id,
            diff_json=diff,
        ))
        await db.commit()
        await db.refresh(case_study)

    return CaseStudyResponse.model_validate(case_study)
