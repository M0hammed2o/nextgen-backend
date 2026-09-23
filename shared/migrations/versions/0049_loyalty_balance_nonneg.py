"""Add CHECK constraint: customers.loyalty_points_balance must never go negative

Revision ID: 0049_loyalty_balance_nonneg
Revises: 0048_wastage_rejection
Create Date: 2026-09-23

BUG-0097 (KNOWN_BUGS.md): redeem_reward() and award_points_for_paid_order()
both performed an unlocked read-modify-write on
customers.loyalty_points_balance, allowing two concurrent redemptions to
both pass the sufficiency check against the same stale balance and both
write, letting a customer spend the same points twice. The primary fix
(backend/app/loyalty/engine.py, same commit series as this migration) takes
a SELECT ... FOR UPDATE row lock before any read/validate/write of the
balance. This migration adds a database-level backstop CHECK constraint so
that no future code path — including one that forgets to take the lock —
can ever push the balance negative; Postgres enforces this independent of
application-layer correctness.

DATA CHECK PERFORMED BEFORE WRITING THIS MIGRATION: queried this session's
own disposable, freshly-created, positively-identified Postgres container
(docker container nextgen-loyalty-concurrency-testdb, port 55930, migrated
0 -> head) for `SELECT id, business_id, loyalty_points_balance FROM
customers WHERE loyalty_points_balance < 0` — zero rows, because that
container was created fresh for this task and has never held real customer
data. NO live/production database with real customer data was reachable or
positively identifiable in this session (see KNOWN_BUGS.md BUG-0096 for the
documented ambiguity around multiple, non-deterministically-routed Postgres
instances on this dev machine) — connecting to any of them without positive
identification would violate this repo's own irreversible-action discipline.
This migration is therefore verified safe against an EMPTY customers table
only. Before applying it to any environment with real customer data, re-run
the same negative-balance query there first; if it returns any rows, do NOT
apply this migration blindly — surface the negative balances for a human
product decision (do not "fix" the data silently) exactly as the original
task instructions for this change required.

Purely additive (widens nothing, just adds a CHECK), fully reversible,
single small predicate on one existing column. Uses raw op.execute() DDL
(not op.create_check_constraint()) to avoid the double constraint-name
prefixing bug documented in KNOWN_BUGS.md BUG-0095 / migration 0045.
"""
from alembic import op

revision = "0049_loyalty_balance_nonneg"
down_revision = "0048_wastage_rejection"
branch_labels = None
depends_on = None

_TABLE = "customers"
_CONSTRAINT = "ck_customers_loyalty_points_balance_nonneg"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (loyalty_points_balance >= 0)"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
