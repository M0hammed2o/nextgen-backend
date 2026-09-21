"""Sales-partner onboarding: ONBOARDER admin role + business ownership stamp

Revision ID: 0034_onboarding_role
Revises: 0031_tenant_nav_stocktake
Create Date: 2026-09-21

MILESTONE B — a sales partner must be able to onboard a restaurant without
developer access and without unrestricted super-admin power.

Adds a second admin_users.role value, ONBOARDER, alongside the existing
SUPER_ADMIN (the column itself already exists — String(32), default
'SUPER_ADMIN' — this migration only constrains its vocabulary in the DB,
the same defense-in-depth pattern as
0031_tenant_nav_stocktake::ck_businesses_stocktake_frequency).

Adds two nullable columns to `businesses` so the platform can tell which
business an ONBOARDER is allowed to touch, and when they stop being allowed
to touch it:
  - onboarded_by_admin_id: which admin created this tenant (NULL for every
    business created directly by a SUPER_ADMIN — the overwhelming majority
    of existing rows).
  - onboarding_completed_at: when handover to the restaurant's own owner
    login happened. NULL means "still in onboarding."

Purely additive and nullable — safe on a live DB, trivially reversible, no
existing row's behaviour changes (every existing business reads as
onboarded_by_admin_id=NULL, onboarding_completed_at=NULL, i.e. "not an
ONBOARDER-owned tenant" — correct, since ONBOARDER did not exist before
this migration).

NOTE (merge instruction from the lead): down_revision is temporarily
0031_tenant_nav_stocktake to allow standalone testing on this branch's
throwaway DB while migrations 0032_*/0033_* land concurrently on another
track. Re-point down_revision to the real chain head at merge time.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Kept under 32 characters: alembic_version.version_num is VARCHAR(32).
revision = "0034_onboarding_role"
down_revision = "0033_order_number_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "onboarded_by_admin_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_businesses_onboarded_by_admin_id_admin_users"),
            nullable=True,
            comment="admin_users.id of the ONBOARDER who created this business; "
                    "NULL for businesses created by a SUPER_ADMIN.",
        ),
    )
    op.create_index(
        "ix_businesses_onboarded_by_admin_id",
        "businesses",
        ["onboarded_by_admin_id"],
    )
    op.add_column(
        "businesses",
        sa.Column(
            "onboarding_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Stamped by POST /admin/businesses/{id}/onboarding/complete. "
                    "Once set, the onboarding ONBOARDER loses access to this business.",
        ),
    )

    # Guard the admin role vocabulary in the database, not just Pydantic —
    # same rationale as ck_businesses_stocktake_frequency: a typo'd role
    # value would silently fall through every Python role check.
    op.create_check_constraint(
        "ck_admin_users_role",
        "admin_users",
        "role IN ('SUPER_ADMIN','ONBOARDER')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_admin_users_role", "admin_users", type_="check")
    op.drop_column("businesses", "onboarding_completed_at")
    op.drop_index("ix_businesses_onboarded_by_admin_id", table_name="businesses")
    op.drop_constraint(
        "fk_businesses_onboarded_by_admin_id_admin_users", "businesses", type_="foreignkey"
    )
    op.drop_column("businesses", "onboarded_by_admin_id")
