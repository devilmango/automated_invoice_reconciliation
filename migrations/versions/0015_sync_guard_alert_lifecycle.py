"""Add guarded sync approvals and complete provider alert lifecycle.

Revision ID: 0015_sync_guard_alert_lifecycle
Revises: 0014_alert_controls
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_sync_guard_alert_lifecycle"
down_revision = "0014_alert_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.add_column(sa.Column("max_deactivations", sa.Integer(), nullable=False, server_default="10"))
        batch_op.add_column(sa.Column("max_diff_records", sa.Integer(), nullable=False, server_default="100"))
        batch_op.add_column(sa.Column("alert_escalation_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("alert_escalation_minutes", sa.Integer(), nullable=False, server_default="1440"))
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("resolved_by_alert_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("resolves_alert_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "master_data_sync_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("risk_summary", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_master_data_sync_approvals_company_id", "master_data_sync_approvals", ["company_id"])
    op.create_index("ix_master_data_sync_approvals_provider", "master_data_sync_approvals", ["provider"])
    op.create_index("ix_master_data_sync_approvals_status", "master_data_sync_approvals", ["status"])
    op.create_index("ix_master_sync_approval_provider_status", "master_data_sync_approvals", ["company_id", "provider", "status"])


def downgrade() -> None:
    op.drop_index("ix_master_sync_approval_provider_status", table_name="master_data_sync_approvals")
    op.drop_index("ix_master_data_sync_approvals_status", table_name="master_data_sync_approvals")
    op.drop_index("ix_master_data_sync_approvals_provider", table_name="master_data_sync_approvals")
    op.drop_index("ix_master_data_sync_approvals_company_id", table_name="master_data_sync_approvals")
    op.drop_table("master_data_sync_approvals")
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_column("escalated_at")
        batch_op.drop_column("resolves_alert_id")
        batch_op.drop_column("resolved_by_alert_id")
        batch_op.drop_column("resolved_at")
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.drop_column("alert_escalation_minutes")
        batch_op.drop_column("alert_escalation_enabled")
        batch_op.drop_column("max_diff_records")
        batch_op.drop_column("max_deactivations")
