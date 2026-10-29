"""Durable provider synchronization for approved credit applications."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    APOutboxRecord,
    AuditEvent,
    CreditApplicationOutboxRecord,
    CreditNoteApplicationRecord,
    MatchRecord,
    utc_now,
)


def sync_credit_applications(session: Session, adapter, *, limit: int = 100, max_attempts: int = 8,
                             initial_retry_seconds: int = 30, max_retry_seconds: int = 3600) -> dict:
    now = utc_now()
    rows = session.scalars(select(CreditApplicationOutboxRecord).where(
        CreditApplicationOutboxRecord.state == "PENDING",
        (CreditApplicationOutboxRecord.next_attempt_at.is_(None)
         | (CreditApplicationOutboxRecord.next_attempt_at <= now)),
    ).order_by(CreditApplicationOutboxRecord.created_at).limit(limit)).all()
    result = {"synced": 0, "failed": 0, "dead_lettered": 0}
    for item in rows:
        item.attempt_count += 1
        application = session.get(CreditNoteApplicationRecord, item.application_id)
        credit = session.get(MatchRecord, application.credit_match_id) if application else None
        invoice = session.get(MatchRecord, application.invoice_match_id) if application else None
        try:
            if not hasattr(adapter, "apply_credit_note"):
                raise RuntimeError(f"{getattr(adapter, 'name', 'Accounting adapter')} does not support credit allocation")
            credit_export = session.get(APOutboxRecord, application.credit_match_id) if application else None
            invoice_export = session.get(APOutboxRecord, application.invoice_match_id) if application else None
            if not all((application, credit, invoice, credit_export, invoice_export)):
                raise RuntimeError("Credit application is missing its source document or AP export")
            if not credit_export.external_reference or not invoice_export.external_reference:
                raise RuntimeError("Credit and invoice provider references are required for allocation")
            external_reference = adapter.apply_credit_note(
                credit_export.external_reference, invoice_export.external_reference,
                Decimal(str(item.amount)), idempotency_key=item.idempotency_key,
            )
            item.state = "ACKNOWLEDGED"
            item.external_reference = str(external_reference or "") or None
            item.acknowledged_at = utc_now()
            item.next_attempt_at = None
            item.last_error = None
            result["synced"] += 1
        except Exception as exc:
            item.last_error = str(exc)[:1000]
            if item.attempt_count >= max_attempts:
                item.state = "DEAD"
                item.next_attempt_at = None
                result["dead_lettered"] += 1
            else:
                retry_after = getattr(exc, "retry_after", None)
                try:
                    delay = max(1, int(retry_after)) if retry_after else min(
                        initial_retry_seconds * (2 ** (item.attempt_count - 1)), max_retry_seconds
                    )
                except (TypeError, ValueError):
                    delay = min(initial_retry_seconds * (2 ** (item.attempt_count - 1)), max_retry_seconds)
                item.next_attempt_at = now + timedelta(seconds=delay)
                result["failed"] += 1
        if credit is not None:
            session.add(AuditEvent(match_id=credit.id, event_type="CREDIT_APPLICATION_SYNC_ATTEMPTED",
                actor=f"{getattr(adapter, 'name', 'accounting')}-worker",
                details={"sync_id": item.id, "invoice_match_id": application.invoice_match_id,
                    "amount": str(item.amount), "attempt": item.attempt_count, "state": item.state,
                    "provider_reference": item.external_reference, "error": item.last_error,
                    "idempotency_key": item.idempotency_key}))
        session.commit()
    return result


def retry_credit_application(item: CreditApplicationOutboxRecord) -> None:
    if item.state not in {"PENDING", "DEAD"}:
        raise ValueError("Only pending or dead credit applications can be retried")
    item.state = "PENDING"
    item.next_attempt_at = datetime.now(timezone.utc)
    item.attempt_count = 0
    item.last_error = None

