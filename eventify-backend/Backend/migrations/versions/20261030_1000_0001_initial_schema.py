"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-30 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("now()")


def _ts(name: str, nullable: bool = False, server_default=NOW) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=None if nullable else server_default)


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="attendee"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("phone", sa.String(30)),
        sa.Column("bio", sa.Text()),
        sa.Column("organization_name", sa.String(150)),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("locked_until", nullable=True),
        _ts("last_login_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("role IN ('attendee', 'organizer', 'admin')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_users_status"),
        sa.CheckConstraint("failed_login_attempts >= 0", name="ck_users_failed_attempts"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_index("ix_users_role_status", "users", ["role", "status"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        _ts("expires_at", server_default=None),
        _ts("revoked_at", nullable=True),
        sa.Column("replaced_by_id", sa.Uuid()),
        _ts("created_at"),
        sa.Column("user_agent", sa.String(255)),
        sa.Column("ip_address", sa.String(45)),
    )
    op.create_index("uq_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        _ts("expires_at", server_default=None),
        _ts("used_at", nullable=True),
        _ts("created_at"),
    )
    op.create_index("uq_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True)
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    # ------------------------------------------------------------------ events
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("cover_image", sa.String(500)),
        sa.Column("organizer_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        _ts("start_datetime", server_default=None),
        _ts("end_datetime", server_default=None),
        _ts("registration_deadline", server_default=None),
        sa.Column("venue", sa.String(255)),
        sa.Column("location", sa.String(255)),
        sa.Column("event_type", sa.String(10), nullable=False, server_default="offline"),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("registered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ticket_type", sa.String(10), nullable=False, server_default="free"),
        sa.Column("ticket_price", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("published_at", nullable=True),
        _ts("reminder_sent_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("capacity > 0 AND capacity <= 100000", name="ck_events_capacity_range"),
        sa.CheckConstraint("registered_count >= 0 AND registered_count <= capacity", name="ck_events_registered_count"),
        sa.CheckConstraint("end_datetime > start_datetime", name="ck_events_dates_order"),
        sa.CheckConstraint("registration_deadline <= start_datetime", name="ck_events_deadline_before_start"),
        sa.CheckConstraint(
            "(ticket_type = 'free' AND ticket_price = 0) OR (ticket_type = 'paid' AND ticket_price > 0)",
            name="ck_events_price_matches_type",
        ),
        sa.CheckConstraint("event_type IN ('online', 'offline')", name="ck_events_event_type"),
        sa.CheckConstraint("ticket_type IN ('free', 'paid')", name="ck_events_ticket_type"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'rejected', 'published', 'cancelled', 'completed')",
            name="ck_events_status",
        ),
    )
    op.create_index("ix_events_status_start", "events", ["status", "start_datetime"])
    op.create_index("ix_events_organizer_status", "events", ["organizer_id", "status"])
    op.create_index("ix_events_category", "events", ["category"])

    op.create_table(
        "event_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reporter_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("details", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolution_note", sa.Text()),
        sa.Column("resolved_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _ts("resolved_at", nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("event_id", "reporter_id", name="uq_event_reports_event_reporter"),
        sa.CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="ck_event_reports_status"),
    )
    op.create_index("ix_event_reports_status", "event_reports", ["status"])

    # ------------------------------------------------------------------ registrations
    op.create_table(
        "registrations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reference", sa.String(24), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        _ts("cancelled_at", nullable=True),
        sa.Column("cancelled_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("status IN ('confirmed', 'cancelled')", name="ck_registrations_status"),
        sa.CheckConstraint("amount >= 0", name="ck_registrations_amount"),
    )
    op.create_index("uq_registrations_reference", "registrations", ["reference"], unique=True)
    op.create_index(
        "uq_registrations_active_user_event",
        "registrations",
        ["user_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_index("ix_registrations_event_status", "registrations", ["event_id", "status"])
    op.create_index("ix_registrations_user_id", "registrations", ["user_id"])

    # ------------------------------------------------------------------ tickets
    op.create_table(
        "tickets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_id", sa.Uuid(), sa.ForeignKey("registrations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_nonce", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="valid"),
        _ts("issued_at"),
        _ts("checked_in_at", nullable=True),
        sa.Column("checked_in_by_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.UniqueConstraint("registration_id", name="uq_tickets_registration_id"),
        sa.CheckConstraint("status IN ('valid', 'used', 'cancelled')", name="ck_tickets_status"),
        sa.CheckConstraint("(status = 'used') = (checked_in_at IS NOT NULL)", name="ck_tickets_checkin_consistent"),
    )
    op.create_index("ix_tickets_event_status", "tickets", ["event_id", "status"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])

    # ------------------------------------------------------------------ reviews / notifications / audit
    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text()),
        _ts("created_at"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_reviews_event_user"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_reviews_rating"),
    )
    op.create_index("ix_reviews_event_id", "reviews", ["event_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="SET NULL")),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at"),
    )
    op.create_index("ix_notifications_user_read_created", "notifications", ["user_id", "is_read", "created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(64)),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("ip_address", sa.String(45)),
        _ts("created_at"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])


def downgrade() -> None:
    for table in (
        "audit_logs",
        "notifications",
        "reviews",
        "tickets",
        "registrations",
        "event_reports",
        "events",
        "password_reset_tokens",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)
