"""Persist mapping suggestion decisions and provider sync schedules.

Revision ID: 0011_mapping_review_sync_schedule
Revises: 0010_credit_sync_bank_review
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_mapping_review_sync_schedule"
down_revision = "0010_credit_sync_bank_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mapping_suggestion_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("source_label", sa.String(255), nullable=False),
        sa.Column("suggested_source_id", sa.String(160), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mapping_suggestion_decisions_company_id", "mapping_suggestion_decisions", ["company_id"])
    op.create_index("ix_mapping_suggestion_decisions_provider", "mapping_suggestion_decisions", ["provider"])
    op.create_index("ix_mapping_suggestion_decisions_category", "mapping_suggestion_decisions", ["category"])
    op.create_index("uq_mapping_suggestion_decision", "mapping_suggestion_decisions",
                    ["company_id", "provider", "category", "source_label", "suggested_source_id"], unique=True)

    op.create_table(
        "master_data_sync_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), server_default="1440", nullable=False),
        sa.Column("stale_after_minutes", sa.Integer(), server_default="2880", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
    )
    op.create_index("ix_master_data_sync_schedules_company_id", "master_data_sync_schedules", ["company_id"])
    op.create_index("ix_master_data_sync_schedules_provider", "master_data_sync_schedules", ["provider"])
    op.create_index("uq_master_data_sync_schedule", "master_data_sync_schedules",
                    ["company_id", "provider"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_master_data_sync_schedule", table_name="master_data_sync_schedules")
    op.drop_index("ix_master_data_sync_schedules_provider", table_name="master_data_sync_schedules")
    op.drop_index("ix_master_data_sync_schedules_company_id", table_name="master_data_sync_schedules")
    op.drop_table("master_data_sync_schedules")
    op.drop_index("uq_mapping_suggestion_decision", table_name="mapping_suggestion_decisions")
    op.drop_index("ix_mapping_suggestion_decisions_category", table_name="mapping_suggestion_decisions")
    op.drop_index("ix_mapping_suggestion_decisions_provider", table_name="mapping_suggestion_decisions")
    op.drop_index("ix_mapping_suggestion_decisions_company_id", table_name="mapping_suggestion_decisions")
    op.drop_table("mapping_suggestion_decisions")
