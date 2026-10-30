"""Add accounting sync history, health, mapping revisions and system alerts.

Revision ID: 0012_accounting_admin_controls
Revises: 0011_mapping_review_sync_schedule
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_accounting_admin_controls"
down_revision = "0011_mapping_review_sync_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "master_data_sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), server_default="RUNNING", nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_master_data_sync_runs_company_id", "master_data_sync_runs", ["company_id"])
    op.create_index("ix_master_data_sync_runs_provider", "master_data_sync_runs", ["provider"])
    op.create_index("ix_master_data_sync_runs_status", "master_data_sync_runs", ["status"])
    op.create_index("ix_master_data_sync_runs_provider_started", "master_data_sync_runs",
                    ["company_id", "provider", "started_at"])

    op.create_table(
        "accounting_connection_health",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_type", sa.String(24), nullable=True),
        sa.Column("account_name", sa.String(255), nullable=True),
        sa.Column("account_id", sa.String(160), nullable=True),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_connection_health_company_id", "accounting_connection_health", ["company_id"])
    op.create_index("ix_accounting_connection_health_provider", "accounting_connection_health", ["provider"])
    op.create_index("uq_accounting_connection_health", "accounting_connection_health",
                    ["company_id", "provider"], unique=True)

    op.create_table(
        "accounting_mapping_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("source_label", sa.String(255), nullable=False),
        sa.Column("previous_source_id", sa.String(160), nullable=True),
        sa.Column("previous_source_label", sa.String(255), nullable=True),
        sa.Column("new_source_id", sa.String(160), nullable=True),
        sa.Column("new_source_label", sa.String(255), nullable=True),
        sa.Column("change_type", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reverts_change_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_mapping_changes_company_id", "accounting_mapping_changes", ["company_id"])
    op.create_index("ix_accounting_mapping_changes_provider", "accounting_mapping_changes", ["provider"])
    op.create_index("ix_accounting_mapping_changes_category", "accounting_mapping_changes", ["category"])
    op.create_index("ix_accounting_mapping_changes_provider_created", "accounting_mapping_changes",
                    ["company_id", "provider", "created_at"])

    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.alter_column("match_id", existing_type=sa.String(36), nullable=True)
        batch_op.add_column(sa.Column("provider", sa.String(24), nullable=True))
    op.create_index("ix_notification_outbox_provider", "notification_outbox", ["provider"])


def downgrade() -> None:
    op.execute("DELETE FROM notification_outbox WHERE match_id IS NULL")
    op.drop_index("ix_notification_outbox_provider", table_name="notification_outbox")
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_column("provider")
        batch_op.alter_column("match_id", existing_type=sa.String(36), nullable=False)
    op.drop_index("ix_accounting_mapping_changes_provider_created", table_name="accounting_mapping_changes")
    op.drop_index("ix_accounting_mapping_changes_category", table_name="accounting_mapping_changes")
    op.drop_index("ix_accounting_mapping_changes_provider", table_name="accounting_mapping_changes")
    op.drop_index("ix_accounting_mapping_changes_company_id", table_name="accounting_mapping_changes")
    op.drop_table("accounting_mapping_changes")
    op.drop_index("uq_accounting_connection_health", table_name="accounting_connection_health")
    op.drop_index("ix_accounting_connection_health_provider", table_name="accounting_connection_health")
    op.drop_index("ix_accounting_connection_health_company_id", table_name="accounting_connection_health")
    op.drop_table("accounting_connection_health")
    op.drop_index("ix_master_data_sync_runs_provider_started", table_name="master_data_sync_runs")
    op.drop_index("ix_master_data_sync_runs_status", table_name="master_data_sync_runs")
    op.drop_index("ix_master_data_sync_runs_provider", table_name="master_data_sync_runs")
    op.drop_index("ix_master_data_sync_runs_company_id", table_name="master_data_sync_runs")
    op.drop_table("master_data_sync_runs")
