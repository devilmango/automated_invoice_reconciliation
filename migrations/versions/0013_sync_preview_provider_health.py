"""Add detailed provider sync diffs and scheduled connection checks.

Revision ID: 0013_sync_preview_provider_health
Revises: 0012_accounting_admin_controls
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_sync_preview_provider_health"
down_revision = "0012_accounting_admin_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("master_data_sync_runs") as batch_op:
        batch_op.add_column(sa.Column("details", sa.JSON(), nullable=False,
                                      server_default=sa.text("'{}'")))
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.add_column(sa.Column("connection_check_enabled", sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column("connection_check_interval_minutes", sa.Integer(), nullable=False,
                                      server_default="60"))
        batch_op.add_column(sa.Column("next_connection_check_at", sa.DateTime(timezone=True), nullable=True,
                                      server_default=sa.func.now()))
        batch_op.add_column(sa.Column("last_connection_check_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.drop_column("last_connection_check_at")
        batch_op.drop_column("next_connection_check_at")
        batch_op.drop_column("connection_check_interval_minutes")
        batch_op.drop_column("connection_check_enabled")
    with op.batch_alter_table("master_data_sync_runs") as batch_op:
        batch_op.drop_column("details")
