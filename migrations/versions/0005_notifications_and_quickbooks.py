"""Add notification delivery and encrypted QuickBooks token storage.

Revision ID: 0005_notifications_qbo
Revises: 0004_rule_versions
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_notifications_qbo"
down_revision = "0004_rule_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
    )
    op.create_index("ix_notification_outbox_match_id", "notification_outbox", ["match_id"])
    op.create_index("ix_notification_outbox_dedupe_key", "notification_outbox", ["dedupe_key"], unique=True)
    op.create_index("ix_notification_outbox_event_type", "notification_outbox", ["event_type"])
    op.create_index("ix_notification_outbox_state", "notification_outbox", ["state"])
    op.create_table(
        "quickbooks_credentials",
        sa.Column("realm_id", sa.String(length=100), primary_key=True),
        sa.Column("encrypted_access_token", sa.String(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.String(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("quickbooks_credentials")
    op.drop_index("ix_notification_outbox_state", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_event_type", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_dedupe_key", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_match_id", table_name="notification_outbox")
    op.drop_table("notification_outbox")
