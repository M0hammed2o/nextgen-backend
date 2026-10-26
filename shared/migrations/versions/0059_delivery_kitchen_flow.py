"""Delivery kitchen flow: provider display id, provider-sync trail, ACCEPT_UNCONFIRMED

Revision ID: 0059_delivery_kitchen_flow
Revises: 0058_business_menu_images
Create Date: 2026-10-26

Purely additive on `delivery_orders` (one small sidecar table), fully
reversible:

* `display_id` — the provider's short, human order code (Uber's
  `display_id`, e.g. "A1B2C"). A courier quotes this code at the counter,
  so the kitchen needs it; it is also the only identifier a provider's
  CSV/report export is likely to carry, so the CSV importer uses it to
  refuse a second sale for an order the live API already booked (TODO.md,
  "CSV Order ID vs live resource_id"). Non-unique index: display codes are
  short and are reused by the provider over time.
* `last_provider_sync_at` / `last_provider_sync_error` — the outcome of the
  most recent outbound status push (ready-for-pickup, restaurant cancel) so
  a failed push is visible on the order instead of only in a log line.
* `ACCEPT_UNCONFIRMED` status — the accept call timed out or failed in a
  way that does not prove the provider rejected it. The sale already
  stands and the kitchen has it, so the accept-deadline watchdog must NOT
  later deny it at the provider (it previously did: the row stayed in
  FETCHING, which the watchdog scans). Deliberately absent from the
  watchdog's status list and from the partial deadline index below.

The status CHECK was last rebuilt by 0045 with raw DDL under its literal
name `ck_delivery_orders_status_known` (0045's own docstring explains the
double-prefix trap on 0042's original name), so that literal name is the one
dropped and re-added here, again with raw DDL.
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_delivery_kitchen_flow"
down_revision = "0058_business_menu_images"
branch_labels = None
depends_on = None

_TABLE = "delivery_orders"
_CONSTRAINT = "ck_delivery_orders_status_known"
_DISPLAY_ID_INDEX = "ix_delivery_orders_business_provider_display_id"

_OLD_STATUS_VALUES = (
    "RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT",
    "ACCEPTED", "DENIED", "FULFILLED", "CANCELLED", "FAILED", "UNKNOWN",
)
_NEW_STATUS_VALUES = _OLD_STATUS_VALUES + ("ACCEPT_UNCONFIRMED",)


def _check_sql(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("display_id", sa.String(64), nullable=True))
    op.add_column(
        _TABLE, sa.Column("last_provider_sync_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("last_provider_sync_error", sa.Text(), nullable=True))
    op.create_index(
        _DISPLAY_ID_INDEX,
        _TABLE,
        ["business_id", "provider", "display_id"],
        postgresql_where=sa.text("display_id IS NOT NULL"),
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK ({_check_sql(_NEW_STATUS_VALUES)})"
    )


def downgrade() -> None:
    # An ACCEPT_UNCONFIRMED row has a real sale behind it and must stay out of
    # the watchdog's scan; FAILED is the pre-0059 status with that property.
    op.execute(
        f"UPDATE {_TABLE} SET status = 'FAILED', "
        f"failure_reason = COALESCE(failure_reason, 'accept unconfirmed (downgraded from 0059)') "
        f"WHERE status = 'ACCEPT_UNCONFIRMED'"
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK ({_check_sql(_OLD_STATUS_VALUES)})"
    )
    op.drop_index(_DISPLAY_ID_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "last_provider_sync_error")
    op.drop_column(_TABLE, "last_provider_sync_at")
    op.drop_column(_TABLE, "display_id")
