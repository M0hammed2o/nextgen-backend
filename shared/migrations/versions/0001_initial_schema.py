"""Initial schema — all tables

Revision ID: 0001
Revises: None
Create Date: 2026-02-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── businesses ───────────────────────────────────────────────────────
    op.create_table(
        "businesses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), unique=True, nullable=False),
        sa.Column("business_code", sa.String(6), unique=True, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("suspended_reason", sa.Text, nullable=True),
        sa.Column("whatsapp_phone_number_id", sa.String(64), unique=True, nullable=True),
        sa.Column("whatsapp_business_account_id", sa.String(64), nullable=True),
        sa.Column("whatsapp_access_token_encrypted", sa.Text, nullable=True),
        sa.Column("last_webhook_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), default="Africa/Johannesburg", nullable=False),
        sa.Column("business_hours", postgresql.JSONB, nullable=True),
        sa.Column("greeting_text", sa.Text, nullable=True),
        sa.Column("fallback_text", sa.Text, nullable=True),
        sa.Column("closed_text", sa.Text, nullable=True),
        sa.Column("order_in_only", sa.Boolean, default=False, nullable=False),
        sa.Column("delivery_enabled", sa.Boolean, default=False, nullable=False),
        sa.Column("delivery_fee_cents", sa.Integer, default=0, nullable=False),
        sa.Column("require_customer_name", sa.Boolean, default=False),
        sa.Column("require_phone_number", sa.Boolean, default=True),
        sa.Column("require_delivery_address", sa.Boolean, default=True),
        sa.Column("currency", sa.String(3), default="ZAR", nullable=False),
        sa.Column("plan", sa.String(32), default="STARTER", nullable=False),
        sa.Column("billing_status", sa.String(32), default="TRIAL", nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_customer_id", sa.String(255), unique=True, nullable=True),
        sa.Column("stripe_subscription_id", sa.String(255), unique=True, nullable=True),
        sa.Column("daily_message_limit", sa.Integer, default=800, nullable=False),
        sa.Column("daily_llm_call_limit", sa.Integer, default=400, nullable=False),
        sa.Column("daily_order_limit", sa.Integer, default=200, nullable=False),
        sa.Column("order_number_sequence", sa.Integer, default=0, nullable=False),
        sa.Column("logo_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("address", sa.Text, nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── admin_users ──────────────────────────────────────────────────────
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), default="SUPER_ADMIN", nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("failed_login_attempts", sa.Integer, default=0, nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── business_users ───────────────────────────────────────────────────
    op.create_table(
        "business_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("email", sa.String(255), unique=True, nullable=True),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("pin_hash", sa.Text, nullable=True),
        sa.Column("pin_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staff_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("failed_login_attempts", sa.Integer, default=0, nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_business_users_business_id", "business_users", ["business_id"])

    # ── refresh_tokens ───────────────────────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("business_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(128), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_info", sa.String(512), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    # ── admin_refresh_tokens ─────────────────────────────────────────────
    op.create_table(
        "admin_refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(128), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_refresh_tokens_user_id", "admin_refresh_tokens", ["user_id"])

    # ── customers ────────────────────────────────────────────────────────
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wa_id", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("phone_number", sa.String(32), nullable=True),
        sa.Column("opted_out", sa.Boolean, default=False, nullable=False),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("business_id", "wa_id", name="uq_customers_business_wa_id"),
    )
    op.create_index("ix_customers_business_id", "customers", ["business_id"])

    # ── conversation_sessions ────────────────────────────────────────────
    op.create_table(
        "conversation_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.String(64), default="IDLE", nullable=False),
        sa.Column("context_json", postgresql.JSONB, nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversation_sessions_business_id", "conversation_sessions", ["business_id"])
    op.create_index("ix_conversation_sessions_customer_id", "conversation_sessions", ["customer_id"])

    # ── menu_categories ──────────────────────────────────────────────────
    op.create_table(
        "menu_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("sort_order", sa.Integer, default=0, nullable=False),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_menu_categories_business_id", "menu_categories", ["business_id"])

    # ── menu_items ───────────────────────────────────────────────────────
    op.create_table(
        "menu_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("menu_categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("price_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), default="ZAR", nullable=False),
        sa.Column("options_json", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("is_deleted", sa.Boolean, default=False, nullable=False),
        sa.Column("sort_order", sa.Integer, default=0, nullable=False),
        sa.Column("image_url", sa.Text, nullable=True),
        sa.Column("image_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_menu_items_business_id", "menu_items", ["business_id"])
    op.create_index("ix_menu_items_category_id", "menu_items", ["category_id"])
    op.create_index("ix_menu_items_active", "menu_items", ["business_id", "is_active", "is_deleted"])

    # ── specials ─────────────────────────────────────────────────────────
    op.create_table(
        "specials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("days_of_week", postgresql.JSONB, nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rule_json", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean, default=True, nullable=False),
        sa.Column("sort_order", sa.Integer, default=0, nullable=False),
        sa.Column("image_url", sa.Text, nullable=True),
        sa.Column("image_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_specials_business_id", "specials", ["business_id"])

    # ── orders ───────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("order_number", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), default="NEW", nullable=False),
        sa.Column("order_mode", sa.String(32), default="PICKUP", nullable=False),
        sa.Column("source", sa.String(32), default="WHATSAPP", nullable=False),
        sa.Column("subtotal_cents", sa.Integer, default=0, nullable=False),
        sa.Column("delivery_fee_cents", sa.Integer, default=0, nullable=False),
        sa.Column("total_cents", sa.Integer, default=0, nullable=False),
        sa.Column("currency", sa.String(3), default="ZAR", nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("phone_number", sa.String(32), nullable=True),
        sa.Column("delivery_address", sa.Text, nullable=True),
        sa.Column("estimated_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_reason", sa.Text, nullable=True),
        sa.Column("cancelled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_orders_business_id", "orders", ["business_id"])
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])
    op.create_index("ix_orders_status", "orders", ["business_id", "status"])
    op.create_index("ix_orders_created_at", "orders", ["business_id", "created_at"])

    # ── order_items ──────────────────────────────────────────────────────
    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("menu_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name_snapshot", sa.String(255), nullable=False),
        sa.Column("unit_price_cents", sa.Integer, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("line_total_cents", sa.Integer, nullable=False),
        sa.Column("options_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("special_instructions", sa.Text, nullable=True),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])

    # ── order_events ─────────────────────────────────────────────────────
    op.create_table(
        "order_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("old_status", sa.String(32), nullable=True),
        sa.Column("new_status", sa.String(32), nullable=False),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_events_order_id", "order_events", ["order_id"])

    # ── messages ─────────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("wa_message_id", sa.String(128), unique=True, nullable=True),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("payload_json", postgresql.JSONB, nullable=True),
        sa.Column("is_llm", sa.Boolean, default=False, nullable=False),
        sa.Column("llm_tokens", sa.Integer, nullable=True),
        sa.Column("llm_cost_cents", sa.Integer, nullable=True),
        sa.Column("llm_provider", sa.String(32), nullable=True),
        sa.Column("intent", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_business_id", "messages", ["business_id"])
    op.create_index("ix_messages_customer_id", "messages", ["customer_id"])

    # ── daily_usage ──────────────────────────────────────────────────────
    op.create_table(
        "daily_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("inbound_messages", sa.Integer, default=0, nullable=False),
        sa.Column("outbound_messages", sa.Integer, default=0, nullable=False),
        sa.Column("llm_calls", sa.Integer, default=0, nullable=False),
        sa.Column("llm_tokens", sa.Integer, default=0, nullable=False),
        sa.Column("llm_cost_cents", sa.Integer, default=0, nullable=False),
        sa.Column("orders_created", sa.Integer, default=0, nullable=False),
        sa.Column("orders_completed", sa.Integer, default=0, nullable=False),
        sa.Column("cancelled_orders", sa.Integer, default=0, nullable=False),
        sa.Column("revenue_cents", sa.Integer, default=0, nullable=False),
        sa.Column("unique_customers", sa.Integer, default=0, nullable=False),
        sa.UniqueConstraint("business_id", "day", name="uq_daily_usage_business_day"),
    )
    op.create_index("ix_daily_usage_business_id", "daily_usage", ["business_id"])

    # ── assets ───────────────────────────────────────────────────────────
    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("file_size_bytes", sa.Integer, nullable=True),
        sa.Column("original_filename", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assets_business_id", "assets", ["business_id"])

    # ── audit_events ─────────────────────────────────────────────────────
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("diff_json", postgresql.JSONB, nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_business_id", "audit_events", ["business_id"])

    # ── message_outbox ───────────────────────────────────────────────────
    op.create_table(
        "message_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_wa_id", sa.String(64), nullable=False),
        sa.Column("message_text", sa.Text, nullable=False),
        sa.Column("payload_json", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(16), default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer, default=0, nullable=False),
        sa.Column("max_attempts", sa.Integer, default=3, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("sent_wa_message_id", sa.String(128), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_outbox_business_id", "message_outbox", ["business_id"])
    op.create_index("ix_message_outbox_status", "message_outbox", ["status", "scheduled_at"])


def downgrade() -> None:
    op.drop_table("message_outbox")
    op.drop_table("audit_events")
    op.drop_table("assets")
    op.drop_table("daily_usage")
    op.drop_table("messages")
    op.drop_table("order_events")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("specials")
    op.drop_table("menu_items")
    op.drop_table("menu_categories")
    op.drop_table("conversation_sessions")
    op.drop_table("customers")
    op.drop_table("admin_refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("business_users")
    op.drop_table("admin_users")
    op.drop_table("businesses")
