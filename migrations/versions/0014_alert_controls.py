"""Add acknowledgement and snooze state for durable provider alerts.

Revision ID: 0014_alert_controls
Revises: 0013_sync_preview_provider_health
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_alert_controls"
down_revision = "0013_sync_preview_provider_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("acknowledged_by", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("snoozed_by", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_column("snoozed_by")
        batch_op.drop_column("snoozed_until")
        batch_op.drop_column("acknowledged_by")
        batch_op.drop_column("acknowledged_at")
