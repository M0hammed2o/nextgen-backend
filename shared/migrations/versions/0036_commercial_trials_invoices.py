"""Commercial trials & EFT/cash invoicing (Wave 0A sales-onboarding push)

Revision ID: 0036_commercial_trials_invoices
Revises: 0035_whatsapp_connections
Create Date: 2026-09-21

Adds:
  - Four purely-additive, nullable-or-defaulted columns on `businesses`:
    trial_started_at, converted_at, trial_extension_days_total (default 0),
    onboarding_confirmations (default '{}'). None of the platform's existing
    stored commercial facts (billing_status, trial_ends_at, is_active,
    suspended_reason) are touched or renamed — see
    backend/app/billing/account_status.py for the single function that
    derives a displayed status from all of these together.
  - A Postgres SEQUENCE (`commercial_invoice_number_seq`) backing sequential
    SA-tax-invoice numbering (`NG-INV-{YYYY}-{seq:05d}`), assigned only when
    a draft invoice is issued — never at draft-creation time, so an
    abandoned draft can never create a gap in the numbering.
  - `commercial_invoices` / `commercial_invoice_lines` — see
    shared/models/commercial_invoice.py for the full field-by-field
    rationale. `commercial_invoices.business_id` is ON DELETE RESTRICT
    (every other business FK in this schema is CASCADE) so a business with
    any invoice history — even a single cancelled draft — can never be
    silently removable; no route in this codebase hard-deletes a Business
    today, so this is deliberate defense-in-depth, not a response to an
    existing gap.

Fully reversible; nothing here alters or drops any existing table/column.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0036_commercial_trials_invoices"
down_revision = "0035_whatsapp_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── businesses: additive trial-tracking columns ─────────────────────
    op.add_column(
        "businesses",
        sa.Column(
            "trial_started_at", sa.DateTime(timezone=True), nullable=True,
            comment="When the current/most recent trial began.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "converted_at", sa.DateTime(timezone=True), nullable=True,
            comment="When this business first paid. Set once, never cleared.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "trial_extension_days_total", sa.Integer(), nullable=False,
            server_default="0",
            comment="Cumulative days this trial has been extended by.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "onboarding_confirmations", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
            comment="Manually-recorded go-live confirmations the system cannot observe.",
        ),
    )

    # ── Sequential invoice numbering ─────────────────────────────────────
    op.execute("CREATE SEQUENCE commercial_invoice_number_seq START WITH 1 INCREMENT BY 1")

    # ── commercial_invoices ───────────────────────────────────────────────
    op.create_table(
        "commercial_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="RESTRICT",
                          name="fk_commercial_invoices_business_id_businesses"),
            nullable=False,
        ),
        sa.Column("invoice_number", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="ZAR"),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vat_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vat_rate_basis_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payment_method", sa.String(8), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String(255), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("payment_note", sa.Text(), nullable=True),
        sa.Column("customer_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("supplier_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_by_admin_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL",
                          name="fk_commercial_invoices_created_by_admin_id_admin_users"),
            nullable=True,
        ),
        sa.Column(
            "paid_recorded_by_admin_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL",
                          name="fk_commercial_invoices_paid_recorded_by_admin_id_admin_users"),
            nullable=True,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_commercial_invoices"),
        sa.UniqueConstraint("invoice_number", name="uq_commercial_invoices_invoice_number"),
        sa.CheckConstraint(
            "status IN ('DRAFT','SENT','AWAITING_PAYMENT','PAID','OVERDUE','CANCELLED')",
            name="ck_commercial_invoices_status",
        ),
        sa.CheckConstraint(
            "payment_method IS NULL OR payment_method IN ('EFT','CASH')",
            name="ck_commercial_invoices_payment_method",
        ),
    )
    op.create_index(
        "ix_commercial_invoices_business_id", "commercial_invoices", ["business_id"],
    )

    # ── commercial_invoice_lines ──────────────────────────────────────────
    op.create_table(
        "commercial_invoice_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("commercial_invoices.id", ondelete="CASCADE",
                          name="fk_commercial_invoice_lines_invoice_id_commercial_invoices"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(16), nullable=False, server_default="OTHER"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit_amount_cents", sa.Integer(), nullable=False),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_commercial_invoice_lines"),
        sa.CheckConstraint(
            "kind IN ('SETUP','SUBSCRIPTION','MODULE','OTHER')",
            name="ck_commercial_invoice_lines_kind",
        ),
    )
    op.create_index(
        "ix_commercial_invoice_lines_invoice_id", "commercial_invoice_lines", ["invoice_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_commercial_invoice_lines_invoice_id", table_name="commercial_invoice_lines")
    op.drop_table("commercial_invoice_lines")
    op.drop_index("ix_commercial_invoices_business_id", table_name="commercial_invoices")
    op.drop_table("commercial_invoices")
    op.execute("DROP SEQUENCE IF EXISTS commercial_invoice_number_seq")
    op.drop_column("businesses", "onboarding_confirmations")
    op.drop_column("businesses", "trial_extension_days_total")
    op.drop_column("businesses", "converted_at")
    op.drop_column("businesses", "trial_started_at")
