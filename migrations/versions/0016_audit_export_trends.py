"""Store verifiable audit export artifacts.

Revision ID: 0016_audit_export_trends
Revises: 0015_sync_guard_alert_lifecycle
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_audit_export_trends"
down_revision = "0015_sync_guard_alert_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_audit_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("generated_by", sa.String(255), nullable=False),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("provider_filter", sa.String(24), nullable=True),
        sa.Column("event_type_filter", sa.String(80), nullable=True),
        sa.Column("actor_filter", sa.String(255), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("file_content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_audit_exports_company_id", "accounting_audit_exports", ["company_id"])
    op.create_index("ix_audit_exports_company_created", "accounting_audit_exports", ["company_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_exports_company_created", table_name="accounting_audit_exports")
    op.drop_index("ix_accounting_audit_exports_company_id", table_name="accounting_audit_exports")
    op.drop_table("accounting_audit_exports")
