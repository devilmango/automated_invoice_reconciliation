"""Bank reconciliation, accounting master data and credit applications.

Revision ID: 0009_bank_master_credit_retry
Revises: 0008_accounting_payments
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_bank_master_credit_retry"
down_revision = "0008_accounting_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ap_outbox", sa.Column("accounting_status_attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ap_outbox", sa.Column("accounting_status_next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ap_outbox", sa.Column("accounting_status_error", sa.String(1000), nullable=True))
    op.create_index("ix_ap_outbox_accounting_status_next_retry_at", "ap_outbox", ["accounting_status_next_retry_at"])

    op.create_table(
        "accounting_master_data",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("code", sa.String(100), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source_data", sa.JSON(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_master_data_company_id", "accounting_master_data", ["company_id"])
    op.create_index("ix_accounting_master_data_provider", "accounting_master_data", ["provider"])
    op.create_index("ix_accounting_master_data_category", "accounting_master_data", ["category"])
    op.create_index("uq_accounting_master_data_source", "accounting_master_data",
                    ["company_id", "provider", "category", "source_id"], unique=True)

    op.create_table(
        "credit_note_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("credit_match_id", sa.String(36), sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("invoice_match_id", sa.String(36), sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credit_note_applications_company_id", "credit_note_applications", ["company_id"])
    op.create_index("ix_credit_note_applications_credit_match_id", "credit_note_applications", ["credit_match_id"])
    op.create_index("ix_credit_note_applications_invoice_match_id", "credit_note_applications", ["invoice_match_id"])
    op.create_index("uq_credit_application_pair", "credit_note_applications",
                    ["company_id", "credit_match_id", "invoice_match_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_credit_application_pair", table_name="credit_note_applications")
    op.drop_index("ix_credit_note_applications_invoice_match_id", table_name="credit_note_applications")
    op.drop_index("ix_credit_note_applications_credit_match_id", table_name="credit_note_applications")
    op.drop_index("ix_credit_note_applications_company_id", table_name="credit_note_applications")
    op.drop_table("credit_note_applications")
    op.drop_index("uq_accounting_master_data_source", table_name="accounting_master_data")
    op.drop_index("ix_accounting_master_data_category", table_name="accounting_master_data")
    op.drop_index("ix_accounting_master_data_provider", table_name="accounting_master_data")
    op.drop_index("ix_accounting_master_data_company_id", table_name="accounting_master_data")
    op.drop_table("accounting_master_data")
    op.drop_index("ix_ap_outbox_accounting_status_next_retry_at", table_name="ap_outbox")
    op.drop_column("ap_outbox", "accounting_status_error")
    op.drop_column("ap_outbox", "accounting_status_next_retry_at")
    op.drop_column("ap_outbox", "accounting_status_attempt_count")
