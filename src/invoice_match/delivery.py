from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .adapters.base import PayablesDeliveryAdapter
from .accounting_connections import check_payload_mappings
from .database import APOutboxRecord, AuditEvent

MAX_AP_ATTEMPTS = 8


def deliver_match(session: Session, adapter: PayablesDeliveryAdapter, match_id: str) -> dict:
    """Retry one pending payable through its configured idempotent adapter."""
    record = session.scalar(
        select(APOutboxRecord).where(APOutboxRecord.match_id == match_id).with_for_update()
    )
    if record is None:
        raise LookupError("AP outbox item not found")
    if record.state not in {"PENDING", "DEAD"}:
        raise ValueError("AP outbox item is already acknowledged")
    issues = check_payload_mappings(adapter, record.payload)
    if issues:
        record.state = "DEAD"
        record.last_error = "Missing accounting mappings: " + ", ".join(
            f"{issue['category']} {issue['source']}" for issue in issues
        )
        record.next_attempt_at = None
        session.add(AuditEvent(match_id=match_id, event_type="AP_EXPORT_BLOCKED_MISSING_MAPPING",
            actor=f"adapter:{adapter.name}", details={"issues": issues}))
        session.commit()
        return {
            "match_id": record.match_id, "state": record.state,
            "attempt_count": record.attempt_count, "last_error": record.last_error,
            "external_reference": record.external_reference,
            "created_at": record.created_at.isoformat(), "acknowledged_at": None,
            "payload": record.payload,
        }
    record.state = "PENDING"
    record.attempt_count += 1
    session.commit()
    try:
        reference = adapter.submit_invoice(record.payload, idempotency_key=match_id)
    except Exception as exc:
        record = session.get(APOutboxRecord, match_id)
        record.last_error = str(exc)[:1000]
        if record.attempt_count >= MAX_AP_ATTEMPTS:
            record.state = "DEAD"
            record.next_attempt_at = None
        else:
            record.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                seconds=min(3600, 30 * (2 ** (record.attempt_count - 1)))
            )
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
        record.next_attempt_at = None
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


def deliver_pending(
    session: Session,
    adapter: PayablesDeliveryAdapter,
    *,
    limit: int = 100,
    now: datetime | None = None,
    max_attempts: int = MAX_AP_ATTEMPTS,
    initial_retry_seconds: int = 30,
    max_retry_seconds: int = 3600,
) -> dict:
    """Deliver due AP entries with bounded exponential retry and a dead-letter state."""
    current = now or datetime.now(timezone.utc)
    summary = {"adapter": adapter.name, "acknowledged": 0, "failed": 0,
               "dead_lettered": 0, "blocked": 0, "items": []}
    while len(summary["items"]) < limit:
        record = session.scalar(
            select(APOutboxRecord).where(
                APOutboxRecord.state == "PENDING",
                (APOutboxRecord.next_attempt_at.is_(None)
                 | (APOutboxRecord.next_attempt_at <= current)),
            ).order_by(APOutboxRecord.created_at.asc()).limit(1).with_for_update(skip_locked=True)
        )
        if record is None:
            break
        issues = check_payload_mappings(adapter, record.payload)
        if issues:
            record.state = "DEAD"
            record.last_error = "Missing accounting mappings: " + ", ".join(
                f"{issue['category']} {issue['source']}" for issue in issues
            )
            record.next_attempt_at = None
            session.add(AuditEvent(match_id=record.match_id,
                event_type="AP_EXPORT_BLOCKED_MISSING_MAPPING", actor=f"adapter:{adapter.name}",
                details={"issues": issues}))
            summary["blocked"] += 1
            summary["items"].append({"match_id": record.match_id, "state": "DEAD",
                                     "error": record.last_error, "blocked": True,
                                     "issues": issues})
            session.commit()
            continue
        record.attempt_count += 1
        try:
            reference = adapter.submit_invoice(record.payload, idempotency_key=record.match_id)
        except Exception as exc:
            record.last_error = str(exc)[:1000]
            if record.attempt_count >= max_attempts:
                record.state = "DEAD"
                record.next_attempt_at = None
                summary["dead_lettered"] += 1
            else:
                delay_seconds = min(max_retry_seconds, initial_retry_seconds * (2 ** (record.attempt_count - 1)))
                record.next_attempt_at = current + timedelta(seconds=delay_seconds)
                summary["failed"] += 1
            session.add(AuditEvent(
                match_id=record.match_id,
                event_type="AP_EXPORT_FAILED",
                actor=f"adapter:{adapter.name}",
                details={"attempt": record.attempt_count, "state": record.state,
                         "retry_at": record.next_attempt_at.isoformat() if record.next_attempt_at else None,
                         "error": record.last_error},
            ))
            summary["items"].append({"match_id": record.match_id, "state": record.state,
                                     "error": record.last_error,
                                     "retry_at": record.next_attempt_at.isoformat() if record.next_attempt_at else None})
        else:
            record.state = "ACKNOWLEDGED"
            record.acknowledged_at = current
            record.external_reference = reference[:255] if reference else None
            record.last_error = None
            record.next_attempt_at = None
            session.add(AuditEvent(
                match_id=record.match_id,
                event_type="AP_EXPORT_ACKNOWLEDGED",
                actor=f"adapter:{adapter.name}",
                details={"external_reference": record.external_reference,
                         "attempt": record.attempt_count},
            ))
            summary["acknowledged"] += 1
            summary["items"].append({"match_id": record.match_id, "state": "ACKNOWLEDGED",
                                     "external_reference": record.external_reference})
        session.commit()
    return summary
