"""Persist synchronized procurement documents.

Revision ID: 0006_procurement_sync
Revises: 0005_notifications_qbo
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_procurement_sync"
down_revision = "0005_notifications_qbo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_records",
        sa.Column("po_number", sa.String(length=100), primary_key=True),
        sa.Column("purchase_order", sa.JSON(), nullable=False),
        sa.Column("receipts", sa.JSON(), nullable=False),
        sa.Column("source_adapter", sa.String(length=40), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "procurement_sync_state",
        sa.Column("source_adapter", sa.String(length=40), primary_key=True),
        sa.Column("last_successful_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_summary", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("procurement_sync_state")
    op.drop_table("procurement_records")
