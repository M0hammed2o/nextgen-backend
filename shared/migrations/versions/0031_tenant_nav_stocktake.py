"""Per-tenant staff navigation + stocktake schedule

Revision ID: 0031_tenant_nav_stocktake
Revises: 0030_external_orders
Create Date: 2026-09-08

Two unrelated-looking needs that are really the same need: a restaurant's
staff app should show that restaurant's workflow, not the platform's average
one.

NAVIGATION
----------
The staff bottom bar has five slots on a phone. A till-only pizzeria wants
Stocktake there and has nothing to watch on Live Orders (a POS sale is
already paid and completed the moment it is rung up); a WhatsApp-ordering
tenant wants the opposite.

These three flags hide TABS, never features. Hiding the wastage tab leaves
the wastage route, its API, its manager reporting and every historical entry
exactly as they were — this is display configuration, not a kill switch, and
nothing about another tenant changes.

`staff_live_orders_tab_enabled` defaults TRUE rather than being derived from
`is_whatsapp_enabled` on purpose. That column governs the BOT; deriving the
staff UI from it would silently remove the tab from any existing tenant that
happens to have the bot switched off, which is a regression dressed up as a
default.

STOCKTAKE SCHEDULE
------------------
How often the owner expects a count (1-4x daily, or custom times). This is
guidance and overdue-flagging only. It deliberately does NOT gate starting a
count: a restaurant that needs to recount at 3pm because a delivery looked
short must always be able to, and a schedule that blocks that would simply
teach staff to work around the system.

Additive only. Every column has a server default matching the behaviour that
existed before this migration, so an un-migrated tenant reads identically.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Kept under 32 characters: alembic_version.version_num is VARCHAR(32).
revision = "0031_tenant_nav_stocktake"
down_revision = "0030_external_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "staff_live_orders_tab_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Show the Live Orders tab in the staff app.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "staff_wastage_tab_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Show the Wastage tab. Hiding it does not disable wastage.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "staff_stocktake_tab_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Show the Stocktake tab. Requires inventory_enabled to be useful.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "stocktake_frequency",
            sa.String(length=16),
            nullable=False,
            server_default="DAILY_1",
            comment="DAILY_1 | DAILY_2 | DAILY_3 | DAILY_4 | CUSTOM",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "stocktake_times_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Local 'HH:MM' times a count is expected.",
        ),
    )

    # Guard the vocabulary in the database rather than only in Pydantic: this
    # column drives an overdue calculation, and a typo'd value would silently
    # make every count look on-time forever.
    op.create_check_constraint(
        "ck_businesses_stocktake_frequency",
        "businesses",
        "stocktake_frequency IN ('DAILY_1','DAILY_2','DAILY_3','DAILY_4','CUSTOM')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_businesses_stocktake_frequency", "businesses", type_="check")
    op.drop_column("businesses", "stocktake_times_json")
    op.drop_column("businesses", "stocktake_frequency")
    op.drop_column("businesses", "staff_stocktake_tab_enabled")
    op.drop_column("businesses", "staff_wastage_tab_enabled")
    op.drop_column("businesses", "staff_live_orders_tab_enabled")
