"""
Tests for the AI Email Outreach case-study profile and the hard constraint
that this module never couples to the `businesses` table (the WhatsApp
ordering product's tenants — a different NextGen Intelligence product,
unrelated to this sales-outreach module).
"""

import pytest
from pydantic import ValidationError

from admin_api.app.schemas.ai_emails import CaseStudyUpdateRequest
from shared.models.ai_emails import AiEmailCaseStudy, AiEmailLead, AiEmailImportBatch


class TestNoBusinessCoupling:
    """
    Enforces the explicit instruction: ai_email_leads (and every other table
    in this module) must have zero FK/relationship to `businesses` or its
    related tables. This is a static metadata check — no DB connection
    needed — so it fails fast in CI if a future change ever adds coupling.
    """

    def _foreign_table_names(self, model) -> set[str]:
        names = set()
        for column in model.__table__.columns:
            for fk in column.foreign_keys:
                names.add(fk.column.table.name)
        return names

    def test_ai_email_lead_has_no_business_fk(self):
        assert "businesses" not in self._foreign_table_names(AiEmailLead)

    def test_ai_email_import_batch_has_no_business_fk(self):
        assert "businesses" not in self._foreign_table_names(AiEmailImportBatch)

    def test_ai_email_case_study_has_no_business_fk(self):
        assert "businesses" not in self._foreign_table_names(AiEmailCaseStudy)

    def test_ai_email_lead_only_references_admin_plane_tables(self):
        # The only FKs this module is allowed: admin_users (assignment/audit)
        # and its own import-batch table. Never businesses/menu/orders/customers.
        allowed = {"admin_users", "ai_email_import_batches"}
        assert self._foreign_table_names(AiEmailLead) <= allowed


class TestCaseStudyModelDefaults:
    """
    SQLAlchemy's Python-level `default=` only applies at INSERT/flush time,
    not on plain instantiation — so these check the column definitions
    directly rather than a freshly constructed (unflushed) instance.
    """

    def _column_default(self, model, column_name):
        return model.__table__.columns[column_name].default.arg

    def test_approval_and_consent_never_default_to_yes(self):
        # Compliance gates a human must explicitly flip — never assumed granted.
        assert self._column_default(AiEmailCaseStudy, "approved_for_marketing") is False
        assert self._column_default(AiEmailCaseStudy, "client_permission_status") == "not_requested"
        assert self._column_default(AiEmailCaseStudy, "screenshot_redaction_status") == "not_applicable"


class TestCaseStudyUpdateRequestValidation:
    def test_valid_permission_status_accepted(self):
        req = CaseStudyUpdateRequest(client_permission_status="granted")
        assert req.client_permission_status == "granted"

    def test_invalid_permission_status_rejected(self):
        with pytest.raises(ValidationError):
            CaseStudyUpdateRequest(client_permission_status="yes_probably")

    def test_invalid_redaction_status_rejected(self):
        with pytest.raises(ValidationError):
            CaseStudyUpdateRequest(screenshot_redaction_status="done")

    def test_partial_update_leaves_other_fields_unset(self):
        req = CaseStudyUpdateRequest(approved_for_marketing=True)
        dumped = req.model_dump(exclude_unset=True)
        assert dumped == {"approved_for_marketing": True}
