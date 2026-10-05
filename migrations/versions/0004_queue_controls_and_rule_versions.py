"""Add queue escalation metadata and versioned matching rules.

Revision ID: 0004_rule_versions
Revises: 0003_capture
Create Date: 2026-10-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_rule_versions"
down_revision = "0003_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(length=255), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rule_versions_version", "rule_versions", ["version"], unique=True)
    op.create_index("ix_rule_versions_digest", "rule_versions", ["digest"])
    op.create_index("ix_rule_versions_status", "rule_versions", ["status"])
    op.create_index(
        "uq_rule_versions_single_active", "rule_versions", ["status"], unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "rule_version_events",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("rule_version_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["rule_version_id"], ["rule_versions.id"]),
    )
    op.create_index("ix_rule_version_events_rule_version_id", "rule_version_events", ["rule_version_id"])
    op.create_index("ix_rule_version_events_event_type", "rule_version_events", ["event_type"])
    op.add_column("matches", sa.Column("rule_version_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("matches") as batch_op:
        batch_op.create_foreign_key(
            "fk_matches_rule_version_id_rule_versions", "rule_versions", ["rule_version_id"], ["id"]
        )
    op.add_column("matches", sa.Column("rule_digest", sa.String(length=64), nullable=False, server_default=""))
    op.add_column("matches", sa.Column("rule_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("matches", sa.Column("escalation_role", sa.String(length=32), nullable=False, server_default="admin"))
    op.add_column("exceptions", sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_exceptions_escalated", "exceptions", ["escalated"])
    op.add_column("exceptions", sa.Column("escalated_to", sa.String(length=32), nullable=True))
    op.add_column("exceptions", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("exceptions", "escalated_at")
    op.drop_column("exceptions", "escalated_to")
    op.drop_index("ix_exceptions_escalated", table_name="exceptions")
    op.drop_column("exceptions", "escalated")
    op.drop_column("matches", "rule_snapshot")
    op.drop_column("matches", "rule_digest")
    op.drop_column("matches", "escalation_role")
    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_constraint("fk_matches_rule_version_id_rule_versions", type_="foreignkey")
    op.drop_column("matches", "rule_version_id")
    op.drop_index("ix_rule_version_events_event_type", table_name="rule_version_events")
    op.drop_index("ix_rule_version_events_rule_version_id", table_name="rule_version_events")
    op.drop_table("rule_version_events")
    op.drop_index("ix_rule_versions_status", table_name="rule_versions")
    op.drop_index("uq_rule_versions_single_active", table_name="rule_versions")
    op.drop_index("ix_rule_versions_digest", table_name="rule_versions")
    op.drop_index("ix_rule_versions_version", table_name="rule_versions")
    op.drop_table("rule_versions")
