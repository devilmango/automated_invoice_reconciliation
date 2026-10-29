"""Add credit application sync queue and account-aware statement review.

Revision ID: 0010_credit_sync_bank_review
Revises: 0009_bank_master_credit_retry
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_credit_sync_bank_review"
down_revision = "0009_bank_master_credit_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credit_application_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("credit_note_applications.id"), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("state", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("external_reference", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credit_application_outbox_company_id", "credit_application_outbox", ["company_id"])
    op.create_index("ix_credit_application_outbox_application_id", "credit_application_outbox", ["application_id"])
    op.create_index("ix_credit_application_outbox_state", "credit_application_outbox", ["state"])
    op.create_index("ix_credit_application_outbox_application", "credit_application_outbox",
                    ["company_id", "application_id"])
    op.create_index("ix_credit_application_outbox_due", "credit_application_outbox",
                    ["state", "next_attempt_at"])
    op.create_index("uq_credit_application_outbox_idempotency", "credit_application_outbox",
                    ["company_id", "idempotency_key"], unique=True)

    op.create_table(
        "bank_statement_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("bank_account_id", sa.String(160), nullable=False),
        sa.Column("statement_id", sa.String(160), nullable=False),
        sa.Column("format", sa.String(12), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("opening_balance", sa.Numeric(18, 4), nullable=True),
        sa.Column("closing_balance", sa.Numeric(18, 4), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("transaction_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bank_statement_batches_company_id", "bank_statement_batches", ["company_id"])
    op.create_index("ix_bank_statement_batches_bank_account_id", "bank_statement_batches", ["bank_account_id"])
    op.create_index("uq_bank_statement_batch", "bank_statement_batches",
                    ["company_id", "bank_account_id", "statement_id"], unique=True)

    op.create_table(
        "bank_account_configurations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("bank_account_id", sa.String(160), nullable=False),
        sa.Column("bai2_transaction_codes", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bank_account_configurations_company_id", "bank_account_configurations", ["company_id"])
    op.create_index("ix_bank_account_configurations_bank_account_id", "bank_account_configurations", ["bank_account_id"])
    op.create_index("uq_bank_account_configuration", "bank_account_configurations",
                    ["company_id", "bank_account_id"], unique=True)

    with op.batch_alter_table("remittance_payments") as batch_op:
        batch_op.add_column(sa.Column("bank_account_id", sa.String(160), nullable=True))
        batch_op.add_column(sa.Column("bank_transaction_id", sa.String(160), nullable=True))
        batch_op.add_column(sa.Column("bank_statement_batch_id", sa.String(36),
                           sa.ForeignKey("bank_statement_batches.id", name="fk_remittance_bank_statement_batch"),
                           nullable=True))
        batch_op.add_column(sa.Column("matched_remittance_id", sa.String(36),
                           sa.ForeignKey("remittance_payments.id", name="fk_bank_payment_matched_remittance"),
                           nullable=True))
    op.create_index("ix_remittance_payments_bank_account_id", "remittance_payments", ["bank_account_id"])
    op.create_index("ix_remittance_payments_bank_statement_batch_id", "remittance_payments",
                    ["bank_statement_batch_id"])
    op.create_index("ix_remittance_payments_matched_remittance_id", "remittance_payments",
                    ["matched_remittance_id"])
    op.create_index("uq_bank_payment_account_transaction", "remittance_payments",
                    ["company_id", "bank_account_id", "bank_transaction_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_bank_payment_account_transaction", table_name="remittance_payments")
    op.drop_index("ix_remittance_payments_bank_statement_batch_id", table_name="remittance_payments")
    op.drop_index("ix_remittance_payments_matched_remittance_id", table_name="remittance_payments")
    op.drop_index("ix_remittance_payments_bank_account_id", table_name="remittance_payments")
    with op.batch_alter_table("remittance_payments") as batch_op:
        batch_op.drop_column("bank_statement_batch_id")
        batch_op.drop_column("matched_remittance_id")
        batch_op.drop_column("bank_transaction_id")
        batch_op.drop_column("bank_account_id")
    op.drop_index("uq_bank_account_configuration", table_name="bank_account_configurations")
    op.drop_index("ix_bank_account_configurations_bank_account_id", table_name="bank_account_configurations")
    op.drop_index("ix_bank_account_configurations_company_id", table_name="bank_account_configurations")
    op.drop_table("bank_account_configurations")
    op.drop_index("uq_bank_statement_batch", table_name="bank_statement_batches")
    op.drop_index("ix_bank_statement_batches_bank_account_id", table_name="bank_statement_batches")
    op.drop_index("ix_bank_statement_batches_company_id", table_name="bank_statement_batches")
    op.drop_table("bank_statement_batches")
    op.drop_index("ix_credit_application_outbox_due", table_name="credit_application_outbox")
    op.drop_index("uq_credit_application_outbox_idempotency", table_name="credit_application_outbox")
    op.drop_index("ix_credit_application_outbox_application", table_name="credit_application_outbox")
    op.drop_index("ix_credit_application_outbox_state", table_name="credit_application_outbox")
    op.drop_index("ix_credit_application_outbox_application_id", table_name="credit_application_outbox")
    op.drop_index("ix_credit_application_outbox_company_id", table_name="credit_application_outbox")
    op.drop_table("credit_application_outbox")
