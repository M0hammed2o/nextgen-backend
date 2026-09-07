"""External marketplace orders: Uber Eats / Mr D daily report import

Revision ID: 0030_external_orders
Revises: 0029_self_service_onboarding
Create Date: 2026-09-08

Ciao Pizza sells through Uber Eats and Mr D as well as its own till. For the
Friday pilot those sales arrive as a daily CSV/XLSX export rather than an API
feed, but they must consume ingredients through exactly the same recipe and
stock ledger as an in-store sale — otherwise theoretical stock is wrong by
however much the restaurant sells on delivery, and the manager reads the
difference as loss.

An imported sale therefore becomes a REAL `orders` row with an external
`source`. No parallel external-order table exists, deliberately: one
canonical product, one canonical recipe, one ledger, one set of reports.

DUPLICATE PROTECTION
--------------------
`uq_orders_business_source_external_id` is the guard that matters. A
restaurant WILL re-upload yesterday's file by mistake, and that must not
deduct ingredients twice. It is a partial unique index so the millions of
ordinary till orders (which have no external id) do not participate.

Enforced in the database, not in the UI — frontend checks do not survive a
double-click, a retried request, or two managers uploading at once.

Additive only.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_external_orders"
down_revision = "0029_self_service_onboarding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Orders: external channel columns ─────────────────────────────────
    for column, ddl in [
        ("external_order_id", "VARCHAR(128) NULL"),
        ("external_batch_id", "UUID NULL"),
        ("external_placed_at", "TIMESTAMPTZ NULL"),
        ("external_imported_at", "TIMESTAMPTZ NULL"),
        ("external_status", "VARCHAR(48) NULL"),
    ]:
        op.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} {ddl}")

    # THE duplicate-upload guard. Partial, so ordinary till orders (external
    # id NULL) are unaffected and can still be created freely.
    op.execute("""
        CREATE UNIQUE INDEX uq_orders_business_source_external_id
            ON orders (business_id, source, external_order_id)
            WHERE external_order_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_orders_business_source_placed
            ON orders (business_id, source, external_placed_at)
    """)
    op.execute("""
        CREATE INDEX ix_orders_external_batch_id
            ON orders (external_batch_id)
            WHERE external_batch_id IS NOT NULL
    """)

    # ── Import batches (audit) ───────────────────────────────────────────
    op.create_table(
        "external_import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PREVIEW"),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("accepted_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duplicate_orders", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rejected_rows", sa.Integer, nullable=False, server_default="0"),
        sa.Column("orders_imported", sa.Integer, nullable=False, server_default="0"),
        sa.Column("movements_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("gross_sales_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("issues_json", postgresql.JSONB, nullable=True),
        sa.CheckConstraint(
            "platform IN ('UBER_EATS', 'MR_D', 'GENERIC')",
            name="ck_external_import_batches_platform_known",
        ),
        sa.CheckConstraint(
            "status IN ('PREVIEW', 'COMPLETED', 'BLOCKED', 'FAILED')",
            name="ck_external_import_batches_status_known",
        ),
    )
    op.create_index("ix_external_import_batches_business_id", "external_import_batches", ["business_id"])
    op.create_index(
        "ix_external_import_batches_business_platform",
        "external_import_batches", ["business_id", "platform"],
    )
    op.create_index(
        "ix_external_import_batches_business_time",
        "external_import_batches", ["business_id", "uploaded_at"],
    )

    # ── Saved product / modifier mappings ────────────────────────────────
    op.create_table(
        "external_product_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="ITEM"),
        sa.Column("external_text", sa.String(500), nullable=False),
        sa.Column("external_key", sa.String(500), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("option_id", sa.String(64), nullable=True),
        sa.Column("menu_add_on_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("menu_add_ons.id", ondelete="CASCADE"), nullable=True),
        sa.Column("removable_ref_id", sa.String(64), nullable=True),
        sa.Column("is_ignored", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("times_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # Tenant scope is part of the key: business A can never resolve,
        # collide with, or overwrite business B's mapping.
        sa.UniqueConstraint(
            "business_id", "platform", "kind", "external_key",
            name="uq_external_product_mappings_business_platform_kind_key",
        ),
        sa.CheckConstraint(
            "kind IN ('ITEM', 'MODIFIER')",
            name="ck_external_product_mappings_kind_known",
        ),
    )
    op.create_index(
        "ix_external_product_mappings_business_id", "external_product_mappings", ["business_id"]
    )
    op.create_index(
        "ix_external_product_mappings_business_platform",
        "external_product_mappings", ["business_id", "platform"],
    )


def downgrade() -> None:
    op.drop_table("external_product_mappings")
    op.drop_table("external_import_batches")
    op.execute("DROP INDEX IF EXISTS ix_orders_external_batch_id")
    op.execute("DROP INDEX IF EXISTS ix_orders_business_source_placed")
    op.execute("DROP INDEX IF EXISTS uq_orders_business_source_external_id")
    for column in (
        "external_status",
        "external_imported_at",
        "external_placed_at",
        "external_batch_id",
        "external_order_id",
    ):
        op.execute(f"ALTER TABLE orders DROP COLUMN IF EXISTS {column}")
