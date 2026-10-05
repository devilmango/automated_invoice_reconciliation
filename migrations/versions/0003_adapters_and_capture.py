"""Add AP adapter delivery diagnostics and captured document review.

Revision ID: 0003_capture
Revises: 0002_integrations
Create Date: 2026-10-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_capture"
down_revision = "0002_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ap_outbox", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ap_outbox", sa.Column("last_error", sa.String(length=1000), nullable=True))
    op.add_column("ap_outbox", sa.Column("external_reference", sa.String(length=255), nullable=True))
    op.create_table(
        "captured_documents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_data", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("extracted_fields", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.JSON(), nullable=False),
        sa.Column("extraction_notes", sa.JSON(), nullable=False),
        sa.Column("reviewed_invoice", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_captured_documents_source_sha256", "captured_documents", ["source_sha256"], unique=True)
    op.create_index("ix_captured_documents_status", "captured_documents", ["status"])
    op.create_table(
        "document_capture_events",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["captured_documents.id"]),
    )
    op.create_index("ix_document_capture_events_document_id", "document_capture_events", ["document_id"])
    op.create_index("ix_document_capture_events_event_type", "document_capture_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_document_capture_events_event_type", table_name="document_capture_events")
    op.drop_index("ix_document_capture_events_document_id", table_name="document_capture_events")
    op.drop_table("document_capture_events")
    op.drop_index("ix_captured_documents_status", table_name="captured_documents")
    op.drop_index("ix_captured_documents_source_sha256", table_name="captured_documents")
    op.drop_table("captured_documents")
    op.drop_column("ap_outbox", "external_reference")
    op.drop_column("ap_outbox", "last_error")
    op.drop_column("ap_outbox", "attempt_count")
