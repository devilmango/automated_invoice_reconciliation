from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


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


class MatchRecord(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("uq_matches_vendor_invoice_number", "vendor_key", "invoice_number_key", unique=True),
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
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
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


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    queue_status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    discrepancies: Mapped[list] = mapped_column(JSON)
    escalated: Mapped[bool] = mapped_column(default=False, index=True)
    escalated_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class APOutboxRecord(Base):
    __tablename__ = "ap_outbox"

    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CapturedDocumentRecord(Base):
    __tablename__ = "captured_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
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


class DocumentCaptureEvent(Base):
    __tablename__ = "document_capture_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("captured_documents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RuleVersionRecord(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        Index(
            "uq_rule_versions_single_active",
            "status",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(String)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    activated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleVersionEvent(Base):
    __tablename__ = "rule_version_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_version_id: Mapped[str] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NotificationOutboxRecord(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
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


class QuickBooksCredentialRecord(Base):
    __tablename__ = "quickbooks_credentials"

    realm_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_access_token: Mapped[str] = mapped_column(String)
    encrypted_refresh_token: Mapped[str] = mapped_column(String)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
