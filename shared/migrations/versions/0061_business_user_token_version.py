"""Add business_users.token_version (end access tokens on credential change)

Revision ID: 0061_business_user_token_version
Revises: 0060_whatsapp_routing_generation
Create Date: 2026-11-18

Access tokens now carry the user's token_version as the ``tv`` claim, and
the per-request check rejects a mismatch. shared/auth_sessions.py bumps it on
every credential change or deactivation, so a token issued before the change
stops working on its next request instead of at expiry (BUG-0206 follow-up).

Additive and non-locking in practice: a NOT NULL column with a constant
server default is a metadata-only change on Postgres 11+. Every existing row
reads 0, and tokens issued before this deploy have no ``tv`` claim, which the
check treats as 0, so nobody is logged out by the deploy itself.

Downgrade drops the column. Tokens issued while it existed carry a ``tv``
claim that older code ignores, so a rollback is safe; only the instant
cut-off is lost.
"""
import sqlalchemy as sa
from alembic import op

revision = "0061_business_user_token_version"
down_revision = "0060_whatsapp_routing_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "business_users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Bumped on every credential change or deactivation; access tokens carry it "
                    "as the 'tv' claim and are rejected on mismatch (shared/auth_sessions.py)",
        ),
    )


def downgrade() -> None:
    op.drop_column("business_users", "token_version")
