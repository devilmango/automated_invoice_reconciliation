"""Add retry scheduling, company scoping, and Xero credentials.

Revision ID: 0007_company_delivery_xero
Revises: 0006_procurement_sync
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_company_delivery_xero"
down_revision = "0006_procurement_sync"
branch_labels = None
depends_on = None

TABLES = (
    "matches", "exceptions", "audit_events", "ap_outbox", "captured_documents",
    "procurement_records", "procurement_sync_state", "document_capture_events",
    "rule_versions", "rule_version_events", "notification_outbox", "quickbooks_credentials",
)


def _replace_primary_key(table: str, columns: list[str], *, downgrade: bool = False) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            table, recreate="always", naming_convention={"pk": "pk_%(table_name)s"}
        ) as batch:
            batch.drop_constraint(f"pk_{table}", type_="primary")
            batch.create_primary_key(f"pk_{table}", columns)
    else:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"pk_{table}", table, columns)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("company_id", sa.String(length=100), server_default="default", nullable=False))
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])
    _replace_primary_key("procurement_records", ["company_id", "po_number"])
    _replace_primary_key("procurement_sync_state", ["company_id", "source_adapter"])
    op.drop_index("uq_matches_vendor_invoice_number", table_name="matches")
    op.drop_index("ix_matches_idempotency_key", table_name="matches")
    op.create_index("uq_matches_vendor_invoice_number", "matches",
                    ["company_id", "vendor_key", "invoice_number_key"], unique=True)
    op.create_index("ix_matches_idempotency_key", "matches",
                    ["company_id", "idempotency_key"], unique=True)
    op.drop_index("ix_captured_documents_source_sha256", table_name="captured_documents")
    op.create_index("ix_captured_documents_source_sha256", "captured_documents",
                    ["company_id", "source_sha256"], unique=True)
    op.drop_index("ix_notification_outbox_dedupe_key", table_name="notification_outbox")
    op.create_index("ix_notification_outbox_dedupe_key", "notification_outbox",
                    ["company_id", "dedupe_key"], unique=True)
    op.drop_index("ix_rule_versions_version", table_name="rule_versions")
    op.create_index("ix_rule_versions_version", "rule_versions", ["company_id", "version"], unique=True)
    op.drop_index("uq_rule_versions_single_active", table_name="rule_versions")
    op.create_index("uq_rule_versions_single_active", "rule_versions", ["company_id", "status"],
                    unique=True, sqlite_where=sa.text("status = 'ACTIVE'"),
                    postgresql_where=sa.text("status = 'ACTIVE'"))
    op.add_column("ap_outbox", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_ap_outbox_next_attempt_at", "ap_outbox", ["next_attempt_at"])
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(length=100), server_default="default", nullable=False),
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
    )
    op.create_index("ix_worker_heartbeats_company_id", "worker_heartbeats", ["company_id"])
    op.create_index("uq_worker_heartbeats_company_name", "worker_heartbeats",
                    ["company_id", "worker_name"], unique=True)
    op.create_table(
        "xero_credentials",
        sa.Column("company_id", sa.String(length=100), server_default="default", primary_key=True),
        sa.Column("tenant_id", sa.String(length=100), primary_key=True),
        sa.Column("encrypted_access_token", sa.String(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.String(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_xero_credentials_company_id", "xero_credentials", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_xero_credentials_company_id", table_name="xero_credentials")
    op.drop_table("xero_credentials")
    op.drop_index("uq_worker_heartbeats_company_name", table_name="worker_heartbeats")
    op.drop_index("ix_worker_heartbeats_company_id", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    _replace_primary_key("procurement_sync_state", ["source_adapter"])
    _replace_primary_key("procurement_records", ["po_number"])
    op.drop_index("ix_ap_outbox_next_attempt_at", table_name="ap_outbox")
    op.drop_index("uq_rule_versions_single_active", table_name="rule_versions")
    op.create_index("uq_rule_versions_single_active", "rule_versions", ["status"], unique=True,
                    sqlite_where=sa.text("status = 'ACTIVE'"),
                    postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_index("ix_rule_versions_version", table_name="rule_versions")
    op.create_index("ix_rule_versions_version", "rule_versions", ["version"], unique=True)
    op.drop_index("ix_notification_outbox_dedupe_key", table_name="notification_outbox")
    op.create_index("ix_notification_outbox_dedupe_key", "notification_outbox", ["dedupe_key"], unique=True)
    op.drop_index("ix_captured_documents_source_sha256", table_name="captured_documents")
    op.create_index("ix_captured_documents_source_sha256", "captured_documents", ["source_sha256"], unique=True)
    op.drop_index("ix_matches_idempotency_key", table_name="matches")
    op.create_index("ix_matches_idempotency_key", "matches", ["idempotency_key"], unique=True)
    op.drop_index("uq_matches_vendor_invoice_number", table_name="matches")
    op.create_index("uq_matches_vendor_invoice_number", "matches",
                    ["vendor_key", "invoice_number_key"], unique=True)
    op.drop_column("ap_outbox", "next_attempt_at")
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_company_id", table_name=table)
        op.drop_column(table, "company_id")
