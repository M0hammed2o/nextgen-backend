"""Push subscription table for Web Push notifications

Revision ID: 0010_push_subs
Revises: 0009_payment_creds
Create Date: 2026-07-08

Stores browser push subscriptions so staff receive native-style push
notifications when new orders arrive, even when the app is in the background
or the phone is on silent.
"""
from alembic import op


revision = "0010_push_subs"
down_revision = "0009_payment_creds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id     UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
            user_id         UUID NOT NULL REFERENCES business_users(id) ON DELETE CASCADE,
            endpoint        VARCHAR(2048) NOT NULL,
            p256dh          VARCHAR(512) NOT NULL,
            auth            VARCHAR(256) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_push_subscription_endpoint UNIQUE (endpoint)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_push_subscriptions_business_id
            ON push_subscriptions (business_id)
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS push_subscriptions;
    """)
