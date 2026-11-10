from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    AccountingConnectionEventRecord,
    AuditExportCleanupRecord,
    AuditExportRecord,
    AuditExportRetentionRecord,
)

DEFAULT_RETENTION_DAYS = 365


def current_retention(session: Session) -> AuditExportRetentionRecord:
    row = session.scalar(select(AuditExportRetentionRecord).limit(1))
    if row is None:
        row = AuditExportRetentionRecord(retention_days=DEFAULT_RETENTION_DAYS, updated_by="system")
        session.add(row)
        session.flush()
    return row


def cleanup_expired_audit_exports(
    session: Session, *, actor: str = "retention-worker", now: datetime | None = None,
) -> dict:
    """Remove expired CSV bytes while retaining export manifests and a durable cleanup record."""
    settings = current_retention(session)
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(days=settings.retention_days)
    rows = session.scalars(select(AuditExportRecord).where(
        AuditExportRecord.created_at < cutoff,
        AuditExportRecord.file_content.is_not(None),
    ).order_by(AuditExportRecord.created_at.asc())).all()
    cleaned = []
    for export in rows:
        byte_count = len(export.file_content or b"")
        cleaned_at = current
        export.file_content = None
        export.content_deleted_at = cleaned_at
        export.content_deleted_by = actor
        record = AuditExportCleanupRecord(
            id=str(uuid4()), export_id=export.id, cleaned_by=actor, cleaned_at=cleaned_at,
            retention_days=settings.retention_days, checksum_sha256=export.checksum_sha256,
            bytes_removed=byte_count,
        )
        session.add(record)
        session.add(AccountingConnectionEventRecord(
            provider=export.provider_filter or "all", event_type="ACCOUNTING_AUDIT_EXPORT_CLEANED",
            actor=actor, created_at=cleaned_at,
            details={"export_id": export.id, "cleanup_id": record.id,
                     "retention_days": settings.retention_days,
                     "checksum_sha256": export.checksum_sha256, "bytes_removed": byte_count},
        ))
        cleaned.append(export.id)
    session.flush()
    return {"retention_days": settings.retention_days, "cutoff": cutoff.isoformat(),
            "cleaned_count": len(cleaned), "export_ids": cleaned}
