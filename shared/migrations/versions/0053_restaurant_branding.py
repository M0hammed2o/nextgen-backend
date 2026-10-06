"""Restaurant visual branding (UI V2)

Revision ID: 0053_restaurant_branding
Revises: 0052_goods_receipt_idempotency
Create Date: 2026-10-06

Gives each restaurant its own visual identity inside one shared platform.

WHAT THIS DELIBERATELY DOES **NOT** ADD
=======================================
`businesses.logo_asset_id` already exists — it has been in the schema since
`0001_initial_schema`. An audit before writing this migration (see
`docs/UI_V2_AUDIT.md`) found it is completely dormant: no route reads or
writes it, no response schema exposes it, no frontend consumes it. It is a
real, correctly-shaped hook, so the UI V2 programme WIRES IT UP rather than
adding a second logo column beside it. Adding `branding_logo_asset_id` here
would have been the textbook duplicate-implementation mistake this repo's
CLAUDE.md warns about.

The asset pipeline it points into is likewise already live: `assets`
(business-scoped, `kind`/`storage_path`/`content_type`) backed by Supabase
Storage, already accepting a `BUSINESS_LOGO` kind, with a shared
`asset_public_url()` resolver. Nothing new is needed there either.

WHAT IT ADDS, AND WHY EACH ONE EARNS ITS PLACE
==============================================
* `brand_primary_colour` / `brand_accent_colour` — no colour column exists
  anywhere in the schema today (verified by repo-wide search). Stored as
  `#RRGGBB` text rather than three integers: it is what a colour picker
  emits, what CSS consumes, and it keeps "unset" expressible as NULL.
  Accent is nullable because the design deliberately asks a restaurant for
  ONE colour and derives the rest — see `lib/brand.ts`.

* `brand_icon_asset_id` — a square app/monogram source. Genuinely distinct
  from the logo: a wide wordmark logo cannot be cropped to a square tile
  without looking wrong, which is exactly how a restaurant's identity ends
  up mangled in its own product. Points at the same `assets` table.

* `brand_display_name` — the name shown IN THE UI, nullable, falling back to
  `businesses.name`. This exists because `name` is already load-bearing in a
  financial context: it is the client name on NextGen's own commercial
  invoices. A restaurant that trades as "NOOSH" but is registered as
  "Noosh Hospitality Group (Pty) Ltd" needs the short form on the till and
  the registered form on the invoice. Overloading one column for both would
  force a choice between a wrong-looking till and a wrong invoice.

* `brand_theme` — 'LIGHT' | 'DARK' | 'SYSTEM'. NOT NULL with a server
  default of 'LIGHT', because light is the intended default experience and
  a NULL here would mean "no opinion", which the UI would have to resolve to
  'LIGHT' anyway. A CHECK constraint keeps the three values honest at the
  database level rather than trusting every writer.

No index is added. These columns are only ever read via a `businesses` row
that has already been located by primary key or by `business_id` from the
JWT — there is no query that filters or sorts on a brand colour, and an
index that nothing uses is pure write cost.

Every column is nullable or defaulted, so this migration is safe on a table
with existing rows and needs no backfill: an un-branded restaurant simply
renders the premium NextGen fallback theme.
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_restaurant_branding"
down_revision = "0052_goods_receipt_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "brand_primary_colour",
            sa.String(length=9),
            nullable=True,
            comment="Restaurant's primary brand colour as #RRGGBB. NULL = use the "
            "NextGen default theme. Derived variants (hover/active/soft/text-on-brand) "
            "are computed at runtime by the frontend's brand engine, never stored.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_accent_colour",
            sa.String(length=9),
            nullable=True,
            comment="Optional secondary accent as #RRGGBB. NULL = derived from the primary.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_icon_asset_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Square icon/monogram source in `assets`. Separate from "
            "logo_asset_id because a wide wordmark cannot be cropped square without "
            "distorting the restaurant's identity.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_display_name",
            sa.String(length=255),
            nullable=True,
            comment="Short trading name shown in the UI. Falls back to businesses.name, "
            "which stays the legal/registered name used on commercial invoices.",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column(
            "brand_theme",
            sa.String(length=8),
            nullable=False,
            server_default="LIGHT",
            comment="LIGHT | DARK | SYSTEM. Light is the intended default experience.",
        ),
    )
    # NOTE: pass the BARE name. shared/models/base.py defines a naming
    # convention (`ck_%(table_name)s_%(constraint_name)s`), so alembic expands
    # this to `ck_businesses_brand_theme`. Passing the already-prefixed name
    # here produces `ck_businesses_ck_businesses_brand_theme` — which is what
    # happened on the first run of this migration against the test database,
    # and is why it is called out rather than silently corrected.
    op.create_check_constraint(
        "brand_theme",
        "businesses",
        "brand_theme IN ('LIGHT', 'DARK', 'SYSTEM')",
    )


def downgrade() -> None:
    op.drop_constraint("brand_theme", "businesses", type_="check")
    op.drop_column("businesses", "brand_theme")
    op.drop_column("businesses", "brand_display_name")
    op.drop_column("businesses", "brand_icon_asset_id")
    op.drop_column("businesses", "brand_accent_colour")
    op.drop_column("businesses", "brand_primary_colour")
