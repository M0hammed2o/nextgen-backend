"""Add partial UNIQUE index on whatsapp_connections.waba_id

Revision ID: 0037_whatsapp_waba_unique
Revises: 0036_commercial_trials_invoices
Create Date: 2026-09-21

Wave 0A verifier finding F8. waba_id was never unique-constrained, unlike
phone_number_id — two connection rows could (and in principle already can)
share the same waba_id, which makes the account_update webhook handler's
`SELECT ... WHERE waba_id == :waba_id` (backend/app/api/v1/routes_webhook.py
::_handle_account_update_event) ambiguous: `scalar_one_or_none()` raises
`MultipleResultsFound` when more than one row matches, silently dropping
the account-lifecycle event (caught by the handler's broad
`except Exception`) instead of applying it to the right connection.

Partial (WHERE waba_id IS NOT NULL) because NULL is the expected value for
a freshly-created PENDING row before Step 2 (verify_assets) of the connect
flow has run (see connection_service.py's F1 fix) — Postgres already
allows multiple NULLs under a plain UNIQUE index, but partial makes the
intent explicit and matches the existing phone_number_id NULL-handling
comment on the model.

Purely additive, no data changes, fully reversible. Fails loudly (listing
the offending ids) if the table already has any real duplicate waba_id
values in NON-NULL rows — this must never be silently "fixed" by picking
one row and orphaning the other; an operator needs to investigate first.
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_whatsapp_waba_unique"
down_revision = "0036_commercial_trials_invoices"
branch_labels = None
depends_on = None

_INDEX_NAME = "uq_whatsapp_connections_waba_id"
_TABLE = "whatsapp_connections"


def upgrade() -> None:
    conn = op.get_bind()
    dupes = conn.execute(
        sa.text(
            """
            SELECT waba_id, array_agg(id::text) AS ids
            FROM whatsapp_connections
            WHERE waba_id IS NOT NULL
            GROUP BY waba_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if dupes:
        details = "; ".join(f"waba_id={row.waba_id} ids={row.ids}" for row in dupes)
        raise RuntimeError(
            "0037_whatsapp_waba_unique: refusing to add a UNIQUE index — "
            f"duplicate non-NULL waba_id values already exist: {details}. "
            "Investigate and resolve these rows manually before re-running "
            "this migration."
        )

    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["waba_id"],
        unique=True,
        postgresql_where=sa.text("waba_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name=_TABLE)
