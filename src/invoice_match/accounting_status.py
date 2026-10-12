from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .database import APOutboxRecord, AuditEvent


def refresh_accounting_status(session: Session, adapter, item: APOutboxRecord, *, actor: str) -> bool:
    fetch_status = getattr(adapter, "fetch_invoice_status", None)
    if fetch_status is None:
        raise ValueError(f"{adapter.name} does not support accounting status lookups")
    if item.state != "ACKNOWLEDGED" or not item.external_reference:
        raise ValueError("Invoice must be acknowledged and have an external reference")
    details = fetch_status(item.external_reference)
    previous = item.accounting_status
    item.accounting_status = details.get("status", "UNKNOWN")
    item.accounting_status_detail = details
    item.accounting_status_checked_at = datetime.now(timezone.utc)
    changed = item.accounting_status != previous
    if changed:
        session.add(AuditEvent(match_id=item.match_id, event_type="AP_ACCOUNTING_STATUS_UPDATED",
            actor=actor, details={"previous_status": previous, "status": item.accounting_status,
                                  "external_reference": item.external_reference}))
    return changed


def refresh_due_accounting_statuses(session: Session, adapter, *, limit: int = 100,
                                    actor: str = "invoice-match-worker",
                                    minimum_interval: int = 900) -> dict:
    if not hasattr(adapter, "fetch_invoice_status"):
        return {"checked": 0, "changed": 0, "failed": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=minimum_interval)
    items = session.scalars(select(APOutboxRecord).where(
        APOutboxRecord.state == "ACKNOWLEDGED", APOutboxRecord.external_reference.is_not(None),
        or_(APOutboxRecord.accounting_status_checked_at.is_(None),
            APOutboxRecord.accounting_status_checked_at <= cutoff),
    ).order_by(APOutboxRecord.accounting_status_checked_at.asc().nullsfirst()).limit(limit)).all()
    summary = {"checked": 0, "changed": 0, "failed": 0}
    for item in items:
        try:
            summary["changed"] += int(refresh_accounting_status(session, adapter, item, actor=actor))
            summary["checked"] += 1
        except (RuntimeError, ValueError):
            summary["failed"] += 1
    session.commit()
    return summary
