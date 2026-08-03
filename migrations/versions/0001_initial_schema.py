"""Начальная схема: пользователи, каталог, заказы, платежи, устройства,
подписки, узлы, промокоды, поддержка, контент, служебные таблицы.

Revision ID: 0001
Revises:
Create Date: 2026-08-03 11:03:54.591508+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "support", "logist", name="adminrole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("totp_secret_enc", sa.String(length=255), nullable=True),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_ip", sa.String(length=64), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("login", name=op.f("uq_admin_users_login")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_admin_users_tg_id")),
    )
    op.create_table(
        "articles",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "media_type",
            sa.Enum(
                "text",
                "photo",
                "video",
                "document",
                "voice",
                "animation",
                name="mediatype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("file_id", sa.String(length=255), nullable=True),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_published", sa.Boolean(), nullable=False),
        sa.Column("show_support_button", sa.Boolean(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["articles.id"], name=op.f("fk_articles_parent_id_articles"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("slug", name=op.f("uq_articles_slug")),
    )
    op.create_index("ix_articles_parent_id_sort_order", "articles", ["parent_id", "sort_order"], unique=False)
    op.create_table(
        "node_groups",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("max_nodes_per_device", sa.Integer(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_groups")),
        sa.UniqueConstraint("slug", name=op.f("uq_node_groups_slug")),
    )
    op.create_table(
        "nodes",
        sa.Column("remarks", sa.String(length=120), nullable=False),
        sa.Column(
            "protocol",
            sa.Enum("vless_reality", "vless_ws_tls", name="nodeprotocol", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("location", sa.String(length=64), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "disabled", "maintenance", name="nodestatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("devices_count", sa.Integer(), nullable=False),
        sa.Column("is_healthy", sa.Boolean(), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("port > 0 AND port < 65536", name=op.f("ck_nodes_port_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nodes")),
        sa.UniqueConstraint("remarks", name=op.f("uq_nodes_remarks")),
    )
    op.create_index("ix_nodes_status_priority", "nodes", ["status", "priority"], unique=False)
    op.create_table(
        "products",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("subtitle", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("model_code", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("old_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "vat_code",
            sa.Enum("1", "2", "3", "4", "5", "6", name="vatcode", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("allow_preorder", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("photo_file_id", sa.String(length=255), nullable=True),
        sa.Column("photo_url", sa.String(length=512), nullable=True),
        sa.Column("specs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("slug", name=op.f("uq_products_slug")),
    )
    op.create_table(
        "users",
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("language_code", sa.String(length=8), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("block_reason", sa.Text(), nullable=True),
        sa.Column("bot_blocked", sa.Boolean(), nullable=False),
        sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("referrer_id", sa.Integer(), nullable=True),
        sa.Column("utm_source", sa.String(length=64), nullable=True),
        sa.Column("start_payload", sa.String(length=128), nullable=True),
        sa.Column("bonus_days", sa.Integer(), nullable=False),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["referrer_id"], ["users.id"], name=op.f("fk_users_referrer_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_users_tg_id")),
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=False)
    op.create_index("ix_users_referrer_id", "users", ["referrer_id"], unique=False)
    op.create_index("ix_users_username", "users", ["username"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column(
            "actor_type",
            sa.Enum("admin", "system", "bot", "client", name="actortype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("actor_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin_users.id"],
            name=op.f("fk_audit_log_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_action_created_at", "audit_log", ["action", "created_at"], unique=False)
    op.create_index("ix_audit_log_admin_id_created_at", "audit_log", ["admin_id", "created_at"], unique=False)
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"], unique=False)
    op.create_table(
        "broadcasts",
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "media_type",
            sa.Enum(
                "text",
                "photo",
                "video",
                "document",
                "voice",
                "animation",
                name="mediatype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("file_id", sa.String(length=255), nullable=True),
        sa.Column("buttons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("segment", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "scheduled",
                "running",
                "paused",
                "done",
                "cancelled",
                name="broadcaststatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_broadcasts_created_by_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )
    op.create_table(
        "plans",
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("extra_days", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("old_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("discount_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column(
            "vat_code",
            sa.Enum("1", "2", "3", "4", "5", "6", name="vatcode", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("node_group_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_group_id"],
            ["node_groups.id"],
            name=op.f("fk_plans_node_group_id_node_groups"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
        sa.UniqueConstraint("slug", name=op.f("uq_plans_slug")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("updated_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_settings_updated_by_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "tickets",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "in_progress",
                "waiting_client",
                "closed",
                name="ticketstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("topic_id", sa.BigInteger(), nullable=True),
        sa.Column("assigned_admin_id", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["assigned_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_tickets_assigned_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_tickets_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(
        "ix_tickets_status_last_message_at", "tickets", ["status", "last_message_at"], unique=False
    )
    op.create_index("ix_tickets_topic_id", "tickets", ["chat_id", "topic_id"], unique=False)
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"], unique=False)
    op.create_table(
        "activation_code_batches",
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("extra_days", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_activation_code_batches_created_by_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plans.id"],
            name=op.f("fk_activation_code_batches_plan_id_plans"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activation_code_batches")),
    )
    op.create_table(
        "broadcast_targets",
        sa.Column("broadcast_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "blocked",
                name="broadcasttargetstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            name=op.f("fk_broadcast_targets_broadcast_id_broadcasts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_broadcast_targets_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcast_targets")),
        sa.UniqueConstraint("broadcast_id", "user_id", name="uq_broadcast_targets_broadcast_id_user_id"),
    )
    op.create_index(
        "ix_broadcast_targets_broadcast_id_status",
        "broadcast_targets",
        ["broadcast_id", "status"],
        unique=False,
    )
    op.create_table(
        "promo_codes",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column(
            "discount_type",
            sa.Enum("percent", "fixed", name="promodiscounttype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("per_user_limit", sa.Integer(), nullable=False),
        sa.Column("min_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("new_clients_only", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_promo_codes_plan_id_plans"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_promo_codes_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_promo_codes_code")),
    )
    op.create_table(
        "ticket_messages",
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("in", "out", name="messagedirection", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "media_type",
            sa.Enum(
                "text",
                "photo",
                "video",
                "document",
                "voice",
                "animation",
                name="mediatype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("admin_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_id", sa.String(length=255), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin_users.id"],
            name=op.f("fk_ticket_messages_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_messages_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("public_number", sa.String(length=24), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "awaiting_payment",
                "paid",
                "packing",
                "shipped",
                "delivered",
                "done",
                "cancelled",
                "refunded",
                name="orderstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("subtotal", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("discount_total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("delivery_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("promo_code_id", sa.Integer(), nullable=True),
        sa.Column("is_cod", sa.Boolean(), nullable=False),
        sa.Column("customer_name", sa.String(length=200), nullable=False),
        sa.Column("customer_phone", sa.String(length=20), nullable=False),
        sa.Column("customer_city", sa.String(length=120), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("utm_source", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["promo_code_id"],
            ["promo_codes.id"],
            name=op.f("fk_orders_promo_code_id_promo_codes"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("public_number", name=op.f("uq_orders_public_number")),
    )
    op.create_index("ix_orders_status_created_at", "orders", ["status", "created_at"], unique=False)
    op.create_index("ix_orders_user_id", "orders", ["user_id"], unique=False)
    op.create_table(
        "deliveries",
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "cdek", "post", "boxberry", "pickup", name="deliverymethod", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "ready",
                "shipped",
                "in_transit",
                "arrived",
                "delivered",
                "returned",
                name="deliverystatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("zone", sa.String(length=32), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("postal_code", sa.String(length=12), nullable=True),
        sa.Column("pvz_code", sa.String(length=64), nullable=True),
        sa.Column("pvz_address", sa.Text(), nullable=True),
        sa.Column("recipient_name", sa.String(length=200), nullable=False),
        sa.Column("recipient_phone", sa.String(length=20), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("tracking_number", sa.String(length=64), nullable=True),
        sa.Column("tracking_url", sa.String(length=512), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_deliveries_order_id_orders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deliveries")),
        sa.UniqueConstraint("order_id", name=op.f("uq_deliveries_order_id")),
    )
    op.create_table(
        "devices",
        sa.Column("mac", sa.String(length=17), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "assigned",
                "active",
                "revoked",
                "blocked",
                name="devicestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("serial", sa.String(length=64), nullable=True),
        sa.Column("fw_version", sa.String(length=32), nullable=False),
        sa.Column("panel_version", sa.String(length=32), nullable=False),
        sa.Column("secret_enc", sa.String(length=512), nullable=True),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sub_token_hash", sa.String(length=64), nullable=True),
        sa.Column("prev_sub_token_hash", sa.String(length=64), nullable=True),
        sa.Column("prev_sub_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sub_token_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_group_id", sa.Integer(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sub_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_wan_ip", sa.String(length=45), nullable=True),
        sa.Column("wan_proto", sa.String(length=16), nullable=True),
        sa.Column(
            "service_status",
            sa.Enum(
                "unknown",
                "running",
                "stopped",
                "error",
                name="deviceservicestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("uptime_sec", sa.BigInteger(), nullable=False),
        sa.Column("rx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("tx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("load_avg", sa.Float(), nullable=True),
        sa.Column("active_nodes", sa.Integer(), nullable=False),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_group_id"],
            ["node_groups.id"],
            name=op.f("fk_devices_node_group_id_node_groups"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_devices_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_devices_user_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("mac", name=op.f("uq_devices_mac")),
        sa.UniqueConstraint("prev_sub_token_hash", name=op.f("uq_devices_prev_sub_token_hash")),
        sa.UniqueConstraint("sub_token_hash", name=op.f("uq_devices_sub_token_hash")),
    )
    op.create_index("ix_devices_last_heartbeat_at", "devices", ["last_heartbeat_at"], unique=False)
    op.create_index("ix_devices_order_id", "devices", ["order_id"], unique=False)
    op.create_index("ix_devices_status", "devices", ["status"], unique=False)
    op.create_index("ix_devices_user_id", "devices", ["user_id"], unique=False)
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column(
            "item_type",
            sa.Enum("product", "plan", "delivery", name="orderitemtype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("total_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "vat_code",
            sa.Enum("1", "2", "3", "4", "5", "6", name="vatcode", native_enum=False, length=8),
            nullable=False,
        ),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_order_items_order_id_orders"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_order_items_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_order_items_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"], unique=False)
    op.create_table(
        "promo_usages",
        sa.Column("promo_code_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("amount_discounted", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_promo_usages_order_id_orders"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["promo_code_id"],
            ["promo_codes.id"],
            name=op.f("fk_promo_usages_promo_code_id_promo_codes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_promo_usages_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promo_usages")),
        sa.UniqueConstraint("promo_code_id", "order_id", name="uq_promo_usages_promo_code_id_order_id"),
    )
    op.create_index("ix_promo_usages_user_id", "promo_usages", ["user_id"], unique=False)
    op.create_table(
        "referrals",
        sa.Column("referrer_id", sa.Integer(), nullable=False),
        sa.Column("referred_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "rewarded", "rejected", name="referralstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("reward_days", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_referrals_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["referred_id"], ["users.id"], name=op.f("fk_referrals_referred_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["referrer_id"], ["users.id"], name=op.f("fk_referrals_referrer_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_referrals")),
        sa.UniqueConstraint("referred_id", name=op.f("uq_referrals_referred_id")),
    )
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"], unique=False)
    op.create_table(
        "activation_codes",
        sa.Column("code", sa.String(length=14), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("extra_days", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "issued",
                "used",
                "expired",
                "revoked",
                name="activationcodestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("used_by_user_id", sa.Integer(), nullable=True),
        sa.Column("used_device_id", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["activation_code_batches.id"],
            name=op.f("fk_activation_codes_batch_id_activation_code_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_activation_codes_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_activation_codes_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["used_by_user_id"],
            ["users.id"],
            name=op.f("fk_activation_codes_used_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["used_device_id"],
            ["devices.id"],
            name=op.f("fk_activation_codes_used_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activation_codes")),
        sa.UniqueConstraint("code", name=op.f("uq_activation_codes_code")),
    )
    op.create_index("ix_activation_codes_batch_id", "activation_codes", ["batch_id"], unique=False)
    op.create_index("ix_activation_codes_status", "activation_codes", ["status"], unique=False)
    op.create_table(
        "device_commands",
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column(
            "command",
            sa.Enum(
                "update_subscription",
                "restart_service",
                "reboot",
                "update_panel",
                "set_config",
                "revoke",
                name="commandtype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "acked",
                "failed",
                "expired",
                name="commandstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"],
            ["admin_users.id"],
            name=op.f("fk_device_commands_created_by_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_commands_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_commands")),
    )
    op.create_index(
        "ix_device_commands_device_id_status", "device_commands", ["device_id", "status"], unique=False
    )
    op.create_table(
        "heartbeats",
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("uptime_sec", sa.BigInteger(), nullable=False),
        sa.Column("fw_version", sa.String(length=32), nullable=False),
        sa.Column("panel_version", sa.String(length=32), nullable=False),
        sa.Column("wan_ip", sa.String(length=45), nullable=True),
        sa.Column("wan_proto", sa.String(length=16), nullable=True),
        sa.Column(
            "service_status",
            sa.Enum(
                "unknown",
                "running",
                "stopped",
                "error",
                name="deviceservicestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("active_nodes", sa.Integer(), nullable=False),
        sa.Column("rx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("tx_bytes", sa.BigInteger(), nullable=False),
        sa.Column("load_avg", sa.Float(), nullable=True),
        sa.Column("remote_ip", sa.String(length=45), nullable=True),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"], ["devices.id"], name=op.f("fk_heartbeats_device_id_devices"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_heartbeats")),
    )
    op.create_index(
        "ix_heartbeats_device_id_created_at", "heartbeats", ["device_id", "created_at"], unique=False
    )
    op.create_table(
        "node_assignments",
        sa.Column("node_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "(group_id IS NOT NULL AND device_id IS NULL) OR (group_id IS NULL AND device_id IS NOT NULL)",
            name=op.f("ck_node_assignments_group_xor_device"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_node_assignments_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["node_groups.id"],
            name=op.f("fk_node_assignments_group_id_node_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_node_assignments_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_assignments")),
        sa.UniqueConstraint("node_id", "device_id", name="uq_node_assignments_node_id_device_id"),
        sa.UniqueConstraint("node_id", "group_id", name="uq_node_assignments_node_id_group_id"),
    )
    op.create_index("ix_node_assignments_device_id", "node_assignments", ["device_id"], unique=False)
    op.create_index("ix_node_assignments_group_id", "node_assignments", ["group_id"], unique=False)
    op.create_table(
        "subscription_access_log",
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("remote_ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("nodes_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_subscription_access_log_device_id_devices"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_access_log")),
    )
    op.create_index(
        "ix_subscription_access_log_device_id_created_at",
        "subscription_access_log",
        ["device_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "subscriptions",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "grace",
                "expired",
                "cancelled",
                name="subscriptionstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False),
        sa.Column("auto_renew_token", sa.String(length=255), nullable=True),
        sa.Column("last_reminder_day", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_subscriptions_device_id_devices"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_subscriptions_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_subscriptions_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_subscriptions_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index("ix_subscriptions_device_id", "subscriptions", ["device_id"], unique=False)
    op.create_index("ix_subscriptions_expires_at", "subscriptions", ["expires_at"], unique=False)
    op.create_index(
        "ix_subscriptions_status_expires_at", "subscriptions", ["status", "expires_at"], unique=False
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=False)
    op.create_table(
        "payments",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column(
            "provider",
            sa.Enum(
                "yookassa",
                "sbp",
                "cryptobot",
                "cod",
                "manual",
                name="paymentprovidername",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "purpose",
            sa.Enum("order", "subscription", name="paymentpurpose", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "waiting_for_capture",
                "succeeded",
                "canceled",
                "failed",
                "refunded",
                name="paymentstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("refunded_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("confirmation_url", sa.String(length=1024), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_webhook", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name=op.f("fk_payments_plan_id_plans"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_payments_subscription_id_subscriptions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_payments_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_payments_idempotency_key")),
        sa.UniqueConstraint("provider_payment_id", name=op.f("uq_payments_provider_payment_id")),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=False)
    op.create_index("ix_payments_status_created_at", "payments", ["status", "created_at"], unique=False)
    op.create_index("ix_payments_user_id", "payments", ["user_id"], unique=False)
    op.create_table(
        "subscription_events",
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column(
            "event",
            sa.Enum(
                "created",
                "activated",
                "extended",
                "renewed",
                "grace_started",
                "expired",
                "cancelled",
                "manual_adjust",
                "bonus",
                "plan_changed",
                "device_changed",
                name="subscriptioneventtype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("days_delta", sa.Integer(), nullable=False),
        sa.Column("old_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_id", sa.Integer(), nullable=True),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admin_users.id"],
            name=op.f("fk_subscription_events_admin_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_subscription_events_payment_id_payments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_subscription_events_subscription_id_subscriptions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_events")),
    )
    op.create_index(
        "ix_subscription_events_subscription_id", "subscription_events", ["subscription_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_events_subscription_id", table_name="subscription_events")
    op.drop_table("subscription_events")
    op.drop_index("ix_payments_user_id", table_name="payments")
    op.drop_index("ix_payments_status_created_at", table_name="payments")
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_status_expires_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_expires_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_device_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_subscription_access_log_device_id_created_at", table_name="subscription_access_log")
    op.drop_table("subscription_access_log")
    op.drop_index("ix_node_assignments_group_id", table_name="node_assignments")
    op.drop_index("ix_node_assignments_device_id", table_name="node_assignments")
    op.drop_table("node_assignments")
    op.drop_index("ix_heartbeats_device_id_created_at", table_name="heartbeats")
    op.drop_table("heartbeats")
    op.drop_index("ix_device_commands_device_id_status", table_name="device_commands")
    op.drop_table("device_commands")
    op.drop_index("ix_activation_codes_status", table_name="activation_codes")
    op.drop_index("ix_activation_codes_batch_id", table_name="activation_codes")
    op.drop_table("activation_codes")
    op.drop_index("ix_referrals_referrer_id", table_name="referrals")
    op.drop_table("referrals")
    op.drop_index("ix_promo_usages_user_id", table_name="promo_usages")
    op.drop_table("promo_usages")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_devices_user_id", table_name="devices")
    op.drop_index("ix_devices_status", table_name="devices")
    op.drop_index("ix_devices_order_id", table_name="devices")
    op.drop_index("ix_devices_last_heartbeat_at", table_name="devices")
    op.drop_table("devices")
    op.drop_table("deliveries")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_index("ix_orders_status_created_at", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_table("promo_codes")
    op.drop_index("ix_broadcast_targets_broadcast_id_status", table_name="broadcast_targets")
    op.drop_table("broadcast_targets")
    op.drop_table("activation_code_batches")
    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_index("ix_tickets_topic_id", table_name="tickets")
    op.drop_index("ix_tickets_status_last_message_at", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("settings")
    op.drop_table("plans")
    op.drop_table("broadcasts")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_admin_id_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_referrer_id", table_name="users")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_table("users")
    op.drop_table("products")
    op.drop_index("ix_nodes_status_priority", table_name="nodes")
    op.drop_table("nodes")
    op.drop_table("node_groups")
    op.drop_index("ix_articles_parent_id_sort_order", table_name="articles")
    op.drop_table("articles")
    op.drop_table("admin_users")
