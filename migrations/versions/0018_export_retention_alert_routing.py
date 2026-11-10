"""Add audit export retention history and provider alert routes.

Revision ID: 0018_export_retention_alert_routing
Revises: 0017_trend_alerts_export_verification
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_export_retention_alert_routing"
down_revision = "0017_trend_alerts_export_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounting_audit_exports") as batch_op:
        batch_op.alter_column("file_content", existing_type=sa.LargeBinary(), nullable=True)
        batch_op.add_column(sa.Column("content_deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("content_deleted_by", sa.String(255), nullable=True))
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.add_column(sa.Column("escalation_acknowledged_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("escalation_acknowledged_by", sa.String(255), nullable=True))
    op.create_table(
        "accounting_audit_export_retention",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="365"),
        sa.Column("updated_by", sa.String(255), nullable=False, server_default="system"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_audit_export_retention_company_id",
                    "accounting_audit_export_retention", ["company_id"])
    op.create_index("uq_audit_export_retention_company", "accounting_audit_export_retention",
                    ["company_id"], unique=True)
    op.create_table(
        "accounting_audit_export_cleanups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("export_id", sa.String(36), nullable=False),
        sa.Column("cleaned_by", sa.String(255), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("bytes_removed", sa.Integer(), nullable=False),
    )
    op.create_index("ix_accounting_audit_export_cleanups_company_id",
                    "accounting_audit_export_cleanups", ["company_id"])
    op.create_index("ix_accounting_audit_export_cleanups_export_id",
                    "accounting_audit_export_cleanups", ["export_id"])
    op.create_index("ix_audit_export_cleanups_company_created",
                    "accounting_audit_export_cleanups", ["company_id", "cleaned_at"])
    op.create_table(
        "admin_alert_routes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("provider_key", sa.String(24), nullable=False, server_default="*"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="ANY"),
        sa.Column("endpoint_url", sa.String(2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_alert_routes_company_id", "admin_alert_routes", ["company_id"])
    op.create_index("uq_admin_alert_routes_company_provider_severity", "admin_alert_routes",
                    ["company_id", "provider_key", "severity"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_admin_alert_routes_company_provider_severity", table_name="admin_alert_routes")
    op.drop_index("ix_admin_alert_routes_company_id", table_name="admin_alert_routes")
    op.drop_table("admin_alert_routes")
    op.drop_index("ix_audit_export_cleanups_company_created", table_name="accounting_audit_export_cleanups")
    op.drop_index("ix_accounting_audit_export_cleanups_export_id", table_name="accounting_audit_export_cleanups")
    op.drop_index("ix_accounting_audit_export_cleanups_company_id", table_name="accounting_audit_export_cleanups")
    op.drop_table("accounting_audit_export_cleanups")
    op.drop_index("ix_accounting_audit_export_retention_company_id", table_name="accounting_audit_export_retention")
    op.drop_index("uq_audit_export_retention_company", table_name="accounting_audit_export_retention")
    op.drop_table("accounting_audit_export_retention")
    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_column("escalation_acknowledged_by")
        batch_op.drop_column("escalation_acknowledged_at")
    with op.batch_alter_table("accounting_audit_exports") as batch_op:
        batch_op.drop_column("content_deleted_by")
        batch_op.drop_column("content_deleted_at")
        batch_op.alter_column("file_content", existing_type=sa.LargeBinary(), nullable=False)
