"""Add provider trend alert settings and audit export verification history.

Revision ID: 0017_trend_alerts_export_verification
Revises: 0016_audit_export_trends
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_trend_alerts_export_verification"
down_revision = "0016_audit_export_trends"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.add_column(sa.Column("trend_alerts_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("trend_window_hours", sa.Integer(), nullable=False, server_default="24"))
        batch_op.add_column(sa.Column("sync_failure_threshold", sa.Integer(), nullable=False, server_default="3"))
        batch_op.add_column(sa.Column("alert_response_threshold_minutes", sa.Integer(), nullable=False, server_default="120"))
        batch_op.add_column(sa.Column("alert_response_min_samples", sa.Integer(), nullable=False, server_default="3"))
        batch_op.add_column(sa.Column("mapping_change_threshold", sa.Integer(), nullable=False, server_default="20"))
        batch_op.add_column(sa.Column("trend_alert_cooldown_minutes", sa.Integer(), nullable=False, server_default="1440"))
    op.create_table(
        "accounting_audit_export_verifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("export_id", sa.String(36), sa.ForeignKey("accounting_audit_exports.id"), nullable=False),
        sa.Column("verified_by", sa.String(255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
    )
    op.create_index("ix_accounting_audit_export_verifications_company_id",
        "accounting_audit_export_verifications", ["company_id"])
    op.create_index("ix_accounting_audit_export_verifications_export_id",
        "accounting_audit_export_verifications", ["export_id"])
    op.create_index("ix_audit_export_verifications_export_created",
        "accounting_audit_export_verifications", ["company_id", "export_id", "verified_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_export_verifications_export_created",
        table_name="accounting_audit_export_verifications")
    op.drop_index("ix_accounting_audit_export_verifications_export_id",
        table_name="accounting_audit_export_verifications")
    op.drop_index("ix_accounting_audit_export_verifications_company_id",
        table_name="accounting_audit_export_verifications")
    op.drop_table("accounting_audit_export_verifications")
    with op.batch_alter_table("master_data_sync_schedules") as batch_op:
        batch_op.drop_column("trend_alert_cooldown_minutes")
        batch_op.drop_column("mapping_change_threshold")
        batch_op.drop_column("alert_response_min_samples")
        batch_op.drop_column("alert_response_threshold_minutes")
        batch_op.drop_column("sync_failure_threshold")
        batch_op.drop_column("trend_window_hours")
        batch_op.drop_column("trend_alerts_enabled")
