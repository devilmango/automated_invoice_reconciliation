from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, Numeric, String, create_engine, text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, with_loader_criteria
from sqlalchemy import event


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./invoice_match.db")


DATABASE_URL = _database_url()
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CompanyScoped:
    company_id: Mapped[str] = mapped_column(String(100), default="default", server_default="default", index=True)


@event.listens_for(Session, "do_orm_execute")
def _scope_company_queries(execute_state) -> None:
    company_id = execute_state.session.info.get("company_id", "default")
    if execute_state.is_select:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                CompanyScoped,
                lambda model: model.company_id == company_id,
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def _assign_company_id(session, flush_context, instances) -> None:
    company_id = session.info.get("company_id", "default")
    for obj in session.new:
        if isinstance(obj, CompanyScoped):
            if obj.company_id not in (None, "default", company_id):
                raise ValueError("Cannot create a record for another company")
            obj.company_id = company_id
    for obj in session.dirty:
        if isinstance(obj, CompanyScoped) and obj.company_id != company_id:
            raise ValueError("Cannot modify a record for another company")


class MatchRecord(CompanyScoped, Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("uq_matches_vendor_invoice_number", "company_id", "vendor_key", "invoice_number_key", unique=True),
        Index("ix_matches_idempotency_key", "company_id", "idempotency_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    approval_status: Mapped[str] = mapped_column(String(16), index=True)
    po_number: Mapped[str] = mapped_column(String(100), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    vendor: Mapped[str] = mapped_column(String(255), index=True)
    vendor_key: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    invoice_number_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval_policy: Mapped[str] = mapped_column(String(100), default="default")
    required_role: Mapped[str] = mapped_column(String(32), default="approver")
    escalation_role: Mapped[str] = mapped_column(String(32), default="admin")
    approvals_required: Mapped[int] = mapped_column(Integer, default=1)
    approvals_received: Mapped[int] = mapped_column(Integer, default=0)
    assigned_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rule_version_id: Mapped[str | None] = mapped_column(ForeignKey("rule_versions.id"), nullable=True)
    rule_digest: Mapped[str] = mapped_column(String(64), default="")
    rule_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    input_data: Mapped[dict] = mapped_column(JSON)
    result_data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExceptionRecord(CompanyScoped, Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    queue_status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    discrepancies: Mapped[list] = mapped_column(JSON)
    escalated: Mapped[bool] = mapped_column(default=False, index=True)
    escalated_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(CompanyScoped, Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class APOutboxRecord(CompanyScoped, Base):
    __tablename__ = "ap_outbox"

    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accounting_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", index=True)
    accounting_status_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    accounting_status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CapturedDocumentRecord(CompanyScoped, Base):
    __tablename__ = "captured_documents"
    __table_args__ = (Index("ix_captured_documents_source_sha256", "company_id", "source_sha256", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_data: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(24), default="REVIEW_REQUIRED", index=True)
    extracted_fields: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[dict] = mapped_column(JSON)
    extraction_notes: Mapped[list] = mapped_column(JSON)
    reviewed_invoice: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcurementRecord(CompanyScoped, Base):
    __tablename__ = "procurement_records"

    company_id: Mapped[str] = mapped_column(String(100), default="default", server_default="default", primary_key=True, index=True)
    po_number: Mapped[str] = mapped_column(String(100), primary_key=True)
    purchase_order: Mapped[dict] = mapped_column(JSON)
    receipts: Mapped[list] = mapped_column(JSON, default=list)
    source_adapter: Mapped[str] = mapped_column(String(40))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProcurementSyncState(CompanyScoped, Base):
    __tablename__ = "procurement_sync_state"

    company_id: Mapped[str] = mapped_column(String(100), default="default", server_default="default", primary_key=True, index=True)
    source_adapter: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_successful_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_summary: Mapped[dict] = mapped_column(JSON, default=dict)


class DocumentCaptureEvent(CompanyScoped, Base):
    __tablename__ = "document_capture_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("captured_documents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RuleVersionRecord(CompanyScoped, Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        Index(
            "uq_rule_versions_single_active",
            "company_id", "status",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_rule_versions_version", "company_id", "version", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(String)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    activated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleVersionEvent(CompanyScoped, Base):
    __tablename__ = "rule_version_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_version_id: Mapped[str] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NotificationOutboxRecord(CompanyScoped, Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (Index("ix_notification_outbox_dedupe_key", "company_id", "dedupe_key", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(24), default="webhook")
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuickBooksCredentialRecord(CompanyScoped, Base):
    __tablename__ = "quickbooks_credentials"

    realm_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(String)
    encrypted_refresh_token: Mapped[str] = mapped_column(String)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class XeroCredentialRecord(CompanyScoped, Base):
    __tablename__ = "xero_credentials"

    company_id: Mapped[str] = mapped_column(String(100), default="default", server_default="default", primary_key=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(String)
    encrypted_refresh_token: Mapped[str] = mapped_column(String)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkerHeartbeatRecord(CompanyScoped, Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (Index("uq_worker_heartbeats_company_name", "company_id", "worker_name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_name: Mapped[str] = mapped_column(String(100))
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class AccountingConnectionRecord(CompanyScoped, Base):
    __tablename__ = "accounting_connections"
    __table_args__ = (Index("uq_accounting_connection_company_provider", "company_id", "provider", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), index=True)
    encrypted_config: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AccountingConnectionEventRecord(CompanyScoped, Base):
    __tablename__ = "accounting_connection_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(24), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RemittancePaymentRecord(CompanyScoped, Base):
    __tablename__ = "remittance_payments"
    __table_args__ = (Index("uq_remittance_payment_company_external", "company_id", "external_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(160))
    vendor: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="NEEDS_REVIEW", index=True)
    input_data: Mapped[dict] = mapped_column(JSON)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentAllocationRecord(CompanyScoped, Base):
    __tablename__ = "payment_allocations"
    __table_args__ = (Index("uq_payment_allocation_payment_match", "company_id", "payment_id", "match_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("remittance_payments.id"), index=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentReconciliationEventRecord(CompanyScoped, Base):
    __tablename__ = "payment_reconciliation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("remittance_payments.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
