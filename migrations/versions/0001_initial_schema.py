"""Create the initial invoice matching schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "matches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approval_status", sa.String(length=16), nullable=False),
        sa.Column("po_number", sa.String(length=100), nullable=False),
        sa.Column("invoice_number", sa.String(length=100), nullable=True),
        sa.Column("vendor", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("result_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matches_status", "matches", ["status"])
    op.create_index("ix_matches_approval_status", "matches", ["approval_status"])
    op.create_index("ix_matches_po_number", "matches", ["po_number"])
    op.create_index("ix_matches_invoice_number", "matches", ["invoice_number"])
    op.create_index("ix_matches_vendor", "matches", ["vendor"])

    op.create_table(
        "exceptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("queue_status", sa.String(length=16), nullable=False),
        sa.Column("discrepancies", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exceptions_match_id", "exceptions", ["match_id"], unique=True)
    op.create_index("ix_exceptions_queue_status", "exceptions", ["queue_status"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_match_id", "audit_events", ["match_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_match_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_exceptions_queue_status", table_name="exceptions")
    op.drop_index("ix_exceptions_match_id", table_name="exceptions")
    op.drop_table("exceptions")
    op.drop_index("ix_matches_vendor", table_name="matches")
    op.drop_index("ix_matches_invoice_number", table_name="matches")
    op.drop_index("ix_matches_po_number", table_name="matches")
    op.drop_index("ix_matches_approval_status", table_name="matches")
    op.drop_index("ix_matches_status", table_name="matches")
    op.drop_table("matches")
