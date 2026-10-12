"""Add managed accounting connections, invoice status, and remittance allocations.

Revision ID: 0008_accounting_payments
Revises: 0007_company_delivery_xero
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_accounting_payments"
down_revision = "0007_company_delivery_xero"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ap_outbox", sa.Column("accounting_status", sa.String(32), server_default="UNKNOWN", nullable=False))
    op.add_column("ap_outbox", sa.Column("accounting_status_detail", sa.JSON(), server_default="{}", nullable=False))
    op.add_column("ap_outbox", sa.Column("accounting_status_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_ap_outbox_accounting_status", "ap_outbox", ["accounting_status"])

    op.create_table(
        "accounting_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("encrypted_config", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_connections_company_id", "accounting_connections", ["company_id"])
    op.create_index("ix_accounting_connections_provider", "accounting_connections", ["provider"])
    op.create_index("ix_accounting_connections_enabled", "accounting_connections", ["enabled"])
    op.create_index("uq_accounting_connection_company_provider", "accounting_connections",
                    ["company_id", "provider"], unique=True)
    op.create_table(
        "accounting_connection_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_connection_events_company_id", "accounting_connection_events", ["company_id"])
    op.create_index("ix_accounting_connection_events_provider", "accounting_connection_events", ["provider"])
    op.create_index("ix_accounting_connection_events_event_type", "accounting_connection_events", ["event_type"])

    op.create_table(
        "remittance_payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("external_id", sa.String(160), nullable=False),
        sa.Column("vendor", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("reference", sa.String(255), nullable=True),
        sa.Column("payment_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("allocated_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_remittance_payments_company_id", "remittance_payments", ["company_id"])
    op.create_index("ix_remittance_payments_status", "remittance_payments", ["status"])
    op.create_index("uq_remittance_payment_company_external", "remittance_payments",
                    ["company_id", "external_id"], unique=True)
    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("match_id", sa.String(36), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["remittance_payments.id"]),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
    )
    op.create_index("ix_payment_allocations_company_id", "payment_allocations", ["company_id"])
    op.create_index("ix_payment_allocations_payment_id", "payment_allocations", ["payment_id"])
    op.create_index("ix_payment_allocations_match_id", "payment_allocations", ["match_id"])
    op.create_index("uq_payment_allocation_payment_match", "payment_allocations",
                    ["company_id", "payment_id", "match_id"], unique=True)
    op.create_table(
        "payment_reconciliation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(100), server_default="default", nullable=False),
        sa.Column("payment_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["remittance_payments.id"]),
    )
    op.create_index("ix_payment_reconciliation_events_company_id", "payment_reconciliation_events", ["company_id"])
    op.create_index("ix_payment_reconciliation_events_payment_id", "payment_reconciliation_events", ["payment_id"])
    op.create_index("ix_payment_reconciliation_events_event_type", "payment_reconciliation_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_payment_reconciliation_events_event_type", table_name="payment_reconciliation_events")
    op.drop_index("ix_payment_reconciliation_events_payment_id", table_name="payment_reconciliation_events")
    op.drop_index("ix_payment_reconciliation_events_company_id", table_name="payment_reconciliation_events")
    op.drop_table("payment_reconciliation_events")
    op.drop_index("uq_payment_allocation_payment_match", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_match_id", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_payment_id", table_name="payment_allocations")
    op.drop_index("ix_payment_allocations_company_id", table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index("uq_remittance_payment_company_external", table_name="remittance_payments")
    op.drop_index("ix_remittance_payments_status", table_name="remittance_payments")
    op.drop_index("ix_remittance_payments_company_id", table_name="remittance_payments")
    op.drop_table("remittance_payments")
    op.drop_index("ix_accounting_connection_events_event_type", table_name="accounting_connection_events")
    op.drop_index("ix_accounting_connection_events_provider", table_name="accounting_connection_events")
    op.drop_index("ix_accounting_connection_events_company_id", table_name="accounting_connection_events")
    op.drop_table("accounting_connection_events")
    op.drop_index("uq_accounting_connection_company_provider", table_name="accounting_connections")
    op.drop_index("ix_accounting_connections_enabled", table_name="accounting_connections")
    op.drop_index("ix_accounting_connections_provider", table_name="accounting_connections")
    op.drop_index("ix_accounting_connections_company_id", table_name="accounting_connections")
    op.drop_table("accounting_connections")
    op.drop_index("ix_ap_outbox_accounting_status", table_name="ap_outbox")
    op.drop_column("ap_outbox", "accounting_status_checked_at")
    op.drop_column("ap_outbox", "accounting_status_detail")
    op.drop_column("ap_outbox", "accounting_status")
