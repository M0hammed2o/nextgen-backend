"""Add order_items.is_ad_hoc_price flag

Revision ID: 0038_order_item_ad_hoc_price
Revises: 0037_whatsapp_waba_unique
Create Date: 2026-09-22

BUG-0016 fix, part 1 of 1 (backend/app/api/v1/routes_orders.py::create_order).

Manual dashboard orders used to trust a client-submitted name+price_cents
for every line item with no validation against the live menu catalog, and
never applied VAT to the order total. The route now resolves any line that
supplies `menu_item_id` against the live catalog via the same
`backend/app/pos/pricing.py::resolve_pos_items` path POS checkout uses, and
applies VAT via `shared/pricing/engine.py::apply_vat_to_total` exactly like
POS. A line with no `menu_item_id` remains a legitimate caller-priced ad
hoc entry (e.g. a hand-typed "delivery surcharge") but needs a durable way
to say so — this column is that flag, read by OrderItemResponse so an
ad-hoc price stays visibly distinct from a catalog-verified one in
receipts/analytics/audit, rather than looking identical to a real
catalog-resolved line.

Additive only — no existing column is altered or dropped. Defaults FALSE
so every existing OrderItem row (all of which predate this distinction) is
treated as non-ad-hoc, which is accurate for every POS/WhatsApp-bot/
external-import row (all already catalog- or provider-resolved) and is a
reasonable, clearly-labelled default for pre-existing manual-dashboard rows
whose real provenance can no longer be distinguished after the fact.
"""
from alembic import op

revision = "0038_order_item_ad_hoc_price"
down_revision = "0037_whatsapp_waba_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE order_items
            ADD COLUMN IF NOT EXISTS is_ad_hoc_price BOOLEAN NOT NULL DEFAULT FALSE
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS is_ad_hoc_price")
