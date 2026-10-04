"""Add idempotency, approval policy state, and the AP outbox.

Revision ID: 0002_integrations
Revises: 0001_initial
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_integrations"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _normalize(value: str | None) -> str | None:
    return " ".join(value.casefold().split()) if value else None


def upgrade() -> None:
    op.add_column("matches", sa.Column("vendor_key", sa.String(length=255), nullable=True))
    op.add_column("matches", sa.Column("invoice_number_key", sa.String(length=100), nullable=True))
    op.add_column("matches", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("matches", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column("matches", sa.Column("cost_center", sa.String(length=100), nullable=True))
    op.add_column("matches", sa.Column("approval_policy", sa.String(length=100), nullable=True))
    op.add_column("matches", sa.Column("required_role", sa.String(length=32), nullable=True))
    op.add_column("matches", sa.Column("approvals_required", sa.Integer(), nullable=True))
    op.add_column("matches", sa.Column("approvals_received", sa.Integer(), nullable=True))
    op.add_column("matches", sa.Column("assigned_to", sa.String(length=255), nullable=True))
    op.add_column("matches", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))

    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT id, vendor, invoice_number FROM matches ORDER BY created_at, id")
    ).mappings()
    seen_invoices: set[tuple[str, str]] = set()
    for row in existing:
        vendor_key = _normalize(row["vendor"]) or ""
        invoice_key = _normalize(row["invoice_number"])
        duplicate_key = (vendor_key, invoice_key) if invoice_key is not None else None
        if duplicate_key is not None and duplicate_key in seen_invoices:
            # Preserve older data; new submissions will match the first canonical row.
            invoice_key = None
        elif duplicate_key is not None:
            seen_invoices.add(duplicate_key)
        connection.execute(
            sa.text(
                "UPDATE matches SET vendor_key=:vendor_key, invoice_number_key=:invoice_key, "
                "approval_policy='default', required_role='approver', approvals_required=1, "
                "approvals_received=0 WHERE id=:match_id"
            ),
            {"vendor_key": vendor_key, "invoice_key": invoice_key, "match_id": row["id"]},
        )

    with op.batch_alter_table("matches") as batch_op:
        batch_op.alter_column("vendor_key", existing_type=sa.String(length=255), nullable=False)
        batch_op.alter_column("approval_policy", existing_type=sa.String(length=100), nullable=False)
        batch_op.alter_column("required_role", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("approvals_required", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("approvals_received", existing_type=sa.Integer(), nullable=False)

    op.create_index("ix_matches_idempotency_key", "matches", ["idempotency_key"], unique=True)
    op.create_index(
        "uq_matches_vendor_invoice_number",
        "matches",
        ["vendor_key", "invoice_number_key"],
        unique=True,
    )

    op.create_table(
        "ap_outbox",
        sa.Column("match_id", sa.String(length=36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"]),
        sa.PrimaryKeyConstraint("match_id"),
    )
    op.create_index("ix_ap_outbox_state", "ap_outbox", ["state"])


def downgrade() -> None:
    op.drop_index("ix_ap_outbox_state", table_name="ap_outbox")
    op.drop_table("ap_outbox")
    op.drop_index("uq_matches_vendor_invoice_number", table_name="matches")
    op.drop_index("ix_matches_idempotency_key", table_name="matches")
    with op.batch_alter_table("matches") as batch_op:
        batch_op.drop_column("due_at")
        batch_op.drop_column("assigned_to")
        batch_op.drop_column("approvals_received")
        batch_op.drop_column("approvals_required")
        batch_op.drop_column("required_role")
        batch_op.drop_column("approval_policy")
        batch_op.drop_column("cost_center")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("invoice_number_key")
        batch_op.drop_column("vendor_key")
