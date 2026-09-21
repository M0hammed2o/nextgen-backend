"""At most one OPEN till session per business

Revision ID: 0032_till_one_open_per_business
Revises: 0031_tenant_nav_stocktake
Create Date: 2026-09-21

Wave 0A / BUG-0042. `till_sessions` (0017) has two plain indexes and no
uniqueness rule, so two simultaneous `POST /business/pos/till/open` calls
both succeed — `_get_open_till` then raises `MultipleResultsFound` and
every checkout 500s until a row is fixed by hand.

Today's product invariant, confirmed by re-reading `_get_open_till` (only
ever filters by `business_id`, no terminal/drawer concept anywhere in the
model or either frontend) and `POST /till/open`'s own pre-check (rejects a
second open outright), is: **at most one OPEN till session per business**.
This migration encodes exactly that — mirroring
`uq_stocktake_sessions_one_open_per_business` (0028).

The upgrade fails loudly, listing every offending business/session, if
duplicate OPEN sessions already exist — it never auto-closes or deletes
anything; that decision belongs to a human who can see which drawer is
the real one.
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_till_one_open_per_business"
down_revision = "0031_tenant_nav_stocktake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dup_rows = conn.execute(sa.text("""
        SELECT business_id, array_agg(id ORDER BY opened_at) AS session_ids, count(*) AS cnt
        FROM till_sessions
        WHERE status = 'OPEN'
        GROUP BY business_id
        HAVING count(*) > 1
    """)).fetchall()
    if dup_rows:
        details = "; ".join(
            f"business_id={r.business_id} has {r.cnt} OPEN till sessions: {r.session_ids}"
            for r in dup_rows
        )
        raise RuntimeError(
            "Cannot add uq_till_sessions_one_open_per_business — duplicate OPEN "
            "till sessions already exist and must be resolved by hand first "
            "(this migration does not auto-close or delete anything; decide "
            "which session is the real drawer, close/cancel the rest, then "
            f"re-run this migration): {details}"
        )

    op.execute("""
        CREATE UNIQUE INDEX uq_till_sessions_one_open_per_business
            ON till_sessions (business_id)
            WHERE status = 'OPEN'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_till_sessions_one_open_per_business")
