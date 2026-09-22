"""Add UNKNOWN to delivery_orders' status CHECK constraint

Revision ID: 0045_delivery_status_unknown
Revises: 0044_delivery_entitlements
Create Date: 2026-09-23

BUG-0094 (item 4, KNOWN_BUGS.md): `DeliveryOrderStatus.UNKNOWN`
(backend/app/delivery/canonical.py) exists specifically for "the provider
reported a state this adapter doesn't recognise — raise an alert rather than
guess" (see `uber/mapping.py::map_current_state`'s own fallback), but
migration 0042's `ck_delivery_orders_status_known` CHECK constraint never
included it. The first code path to actually persist `UNKNOWN` would raise a
CHECK-constraint violation instead of the intended "surfaced for a human"
behaviour.

Purely additive (widens an existing CHECK), fully reversible, single small
table (`delivery_orders`) — same profile as every other migration in this
chain. Does NOT amend migration 0042 in place: per this repo's migration
discipline, 0040-0044 have already been applied to a real (if disposable)
Postgres and are not to be amended again by default (see CLAUDE.md /
KNOWN_BUGS.md BUG-0086's amendment note for the prior, now-closed exception).
"""
from alembic import op

revision = "0045_delivery_status_unknown"
down_revision = "0044_delivery_entitlements"
branch_labels = None
depends_on = None

_TABLE = "delivery_orders"

# migration 0042's `create_table()` call declared this constraint with the
# literal name below via a raw `sa.CheckConstraint(name=...)`:
_CONSTRAINT = "ck_delivery_orders_status_known"
# ...but that is NOT the name Postgres actually stored it under. Confirmed
# by hand against a real, freshly-migrated (0-0044) disposable Postgres
# this session: `SELECT conname FROM pg_constraint WHERE conrelid =
# 'delivery_orders'::regclass` returns
# "ck_delivery_orders_ck_delivery_orders_status_known" (and, for the sibling
# `provider` CHECK, "ck_delivery_orders_ck_delivery_orders_provider_known").
# Root cause: `op.create_table()` builds each declared Constraint via
# `sa.Table(...).append_constraint()` bound to an ephemeral MetaData that
# copies `target_metadata`'s naming convention (shared/models/base.py:
# "ck": "ck_%(table_name)s_%(constraint_name)s"). SQLAlchemy re-templates
# ANY given `name=` through that convention when a constraint is attached
# to a table with a matching convention key — even an already-fully-
# qualified name — producing a DETERMINISTIC (string concatenation, not a
# content hash) double prefix. Migration 0043 shipped with the exact same
# class of bug on its own two CHECK constraints (recorded separately in
# KNOWN_BUGS.md; NOT fixed here — 0043 is an already-applied, already-
# amended-once migration, out of this migration's scope per this repo's
# "don't re-amend" discipline). This migration does not attempt to silently
# correct 0042's naming either — only REVERSING it would touch a migration
# already covered by that same discipline; instead it targets the REAL
# (double-prefixed) name for the DROP half of upgrade()/the ADD half of
# downgrade(), and uses raw `op.execute()` DDL text throughout (which never
# goes through the naming-convention object-construction path at all) so
# this migration's OWN new constraint name is not itself mis-templated.
_CONSTRAINT_AS_ACTUALLY_STORED_BY_0042 = "ck_delivery_orders_ck_delivery_orders_status_known"

_OLD_STATUS_VALUES = (
    "RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT",
    "ACCEPTED", "DENIED", "FULFILLED", "CANCELLED", "FAILED",
)
_NEW_STATUS_VALUES = _OLD_STATUS_VALUES + ("UNKNOWN",)


def _check_sql(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT "{_CONSTRAINT_AS_ACTUALLY_STORED_BY_0042}"')
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK ({_check_sql(_NEW_STATUS_VALUES)})"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{_CONSTRAINT_AS_ACTUALLY_STORED_BY_0042}" '
        f"CHECK ({_check_sql(_OLD_STATUS_VALUES)})"
    )
