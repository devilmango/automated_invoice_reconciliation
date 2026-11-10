"""Add accounting period close locks and sign-off history.

Revision ID: 0019_accounting_period_close
Revises: 0018_export_retention_alert_routing
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_accounting_period_close"
down_revision = "0018_export_retention_alert_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_period_closes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="LOCKED"),
        sa.Column("signed_off_by", sa.String(255), nullable=False),
        sa.Column("signed_off_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signoff_note", sa.String(2000), nullable=False),
        sa.Column("report_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_period_closes_company_id", "accounting_period_closes", ["company_id"])
    op.create_index("ix_accounting_period_closes_period_start", "accounting_period_closes", ["period_start"])
    op.create_index("ix_accounting_period_closes_period_end", "accounting_period_closes", ["period_end"])
    op.create_index("ix_accounting_period_closes_status", "accounting_period_closes", ["company_id", "status"])
    op.create_index("uq_accounting_period_close_range", "accounting_period_closes",
                    ["company_id", "period_start", "period_end"], unique=True)
    op.create_table(
        "accounting_period_close_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.String(100), nullable=False, server_default="default"),
        sa.Column("close_id", sa.String(36), sa.ForeignKey("accounting_period_closes.id"), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounting_period_close_events_company_id",
                    "accounting_period_close_events", ["company_id"])
    op.create_index("ix_accounting_period_close_events_close_id",
                    "accounting_period_close_events", ["close_id"])
    op.create_index("ix_accounting_period_close_events_event_type",
                    "accounting_period_close_events", ["event_type"])
    op.create_index("ix_accounting_period_close_events_close_created",
                    "accounting_period_close_events", ["company_id", "close_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_accounting_period_close_events_close_created", table_name="accounting_period_close_events")
    op.drop_index("ix_accounting_period_close_events_event_type", table_name="accounting_period_close_events")
    op.drop_index("ix_accounting_period_close_events_close_id", table_name="accounting_period_close_events")
    op.drop_index("ix_accounting_period_close_events_company_id", table_name="accounting_period_close_events")
    op.drop_table("accounting_period_close_events")
    op.drop_index("uq_accounting_period_close_range", table_name="accounting_period_closes")
    op.drop_index("ix_accounting_period_closes_status", table_name="accounting_period_closes")
    op.drop_index("ix_accounting_period_closes_period_end", table_name="accounting_period_closes")
    op.drop_index("ix_accounting_period_closes_period_start", table_name="accounting_period_closes")
    op.drop_index("ix_accounting_period_closes_company_id", table_name="accounting_period_closes")
    op.drop_table("accounting_period_closes")
