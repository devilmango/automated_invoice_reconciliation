from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapters.base import PayablesDeliveryAdapter
from .database import APOutboxRecord, AuditEvent


def deliver_match(session: Session, adapter: PayablesDeliveryAdapter, match_id: str) -> dict:
    """Retry one pending payable through its configured idempotent adapter."""
    record = session.scalar(
        select(APOutboxRecord).where(APOutboxRecord.match_id == match_id).with_for_update()
    )
    if record is None:
        raise LookupError("AP outbox item not found")
    if record.state != "PENDING":
        raise ValueError("AP outbox item is already acknowledged")
    record.attempt_count += 1
    session.commit()
    try:
        reference = adapter.submit_invoice(record.payload, idempotency_key=match_id)
    except Exception as exc:
        record = session.get(APOutboxRecord, match_id)
        record.last_error = str(exc)[:1000]
        session.add(AuditEvent(
            match_id=match_id,
            event_type="AP_EXPORT_FAILED",
            actor=f"adapter:{adapter.name}",
            details={"attempt": record.attempt_count, "error": record.last_error},
        ))
        session.commit()
    else:
        record = session.get(APOutboxRecord, match_id)
        record.state = "ACKNOWLEDGED"
        record.acknowledged_at = datetime.now(timezone.utc)
        record.external_reference = reference[:255] if reference else None
        record.last_error = None
        session.add(AuditEvent(
            match_id=match_id,
            event_type="AP_EXPORT_ACKNOWLEDGED",
            actor=f"adapter:{adapter.name}",
            details={"external_reference": record.external_reference, "attempt": record.attempt_count},
        ))
        session.commit()
    return {
        "match_id": record.match_id,
        "state": record.state,
        "attempt_count": record.attempt_count,
        "last_error": record.last_error,
        "external_reference": record.external_reference,
        "created_at": record.created_at.isoformat(),
        "acknowledged_at": record.acknowledged_at.isoformat() if record.acknowledged_at else None,
        "payload": record.payload,
    }


def deliver_pending(session: Session, adapter: PayablesDeliveryAdapter, *, limit: int = 100) -> dict:
    """Send pending outbox payloads and persist success/failure for each item."""
    match_ids = session.scalars(
        select(APOutboxRecord.match_id)
        .where(APOutboxRecord.state == "PENDING")
        .order_by(APOutboxRecord.created_at.asc())
        .limit(limit)
    ).all()
    session.commit()
    summary = {"adapter": adapter.name, "acknowledged": 0, "failed": 0, "items": []}
    for match_id in match_ids:
        record = session.get(APOutboxRecord, match_id)
        if record is None or record.state != "PENDING":
            continue
        record.attempt_count += 1
        try:
            reference = adapter.submit_invoice(record.payload, idempotency_key=match_id)
        except Exception as exc:  # Adapter failures are stored so operators can retry later.
            record.last_error = str(exc)[:1000]
            session.add(AuditEvent(
                match_id=match_id,
                event_type="AP_EXPORT_FAILED",
                actor=f"adapter:{adapter.name}",
                details={"attempt": record.attempt_count, "error": record.last_error},
            ))
            summary["failed"] += 1
            summary["items"].append({"match_id": match_id, "state": "FAILED", "error": record.last_error})
        else:
            record.state = "ACKNOWLEDGED"
            record.acknowledged_at = datetime.now(timezone.utc)
            record.external_reference = reference[:255] if reference else None
            record.last_error = None
            session.add(AuditEvent(
                match_id=match_id,
                event_type="AP_EXPORT_ACKNOWLEDGED",
                actor=f"adapter:{adapter.name}",
                details={"external_reference": record.external_reference},
            ))
            summary["acknowledged"] += 1
            summary["items"].append({
                "match_id": match_id,
                "state": "ACKNOWLEDGED",
                "external_reference": record.external_reference,
            })
        session.commit()
    return summary
