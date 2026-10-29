from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import AccountingConnectionEventRecord, AccountingConnectionRecord, MasterDataSyncScheduleRecord


def sync_due_master_data(session: Session, *, company_id: str, adapter_factory, apply_sync,
                         actor: str, limit: int = 100) -> dict[str, int]:
    """Run due provider syncs and persist success, retry, and audit state."""
    now = datetime.now(timezone.utc)
    due = session.scalars(select(MasterDataSyncScheduleRecord).where(
        MasterDataSyncScheduleRecord.enabled.is_(True),
        MasterDataSyncScheduleRecord.next_run_at <= now,
    ).order_by(MasterDataSyncScheduleRecord.next_run_at).limit(limit)).all()
    summary = {"synced": 0, "failed": 0}
    for schedule in due:
        schedule.last_started_at = now
        connection = session.scalar(select(AccountingConnectionRecord).where(
            AccountingConnectionRecord.provider == schedule.provider,
            AccountingConnectionRecord.enabled.is_(True)))
        try:
            if connection is None:
                raise ValueError("No enabled accounting connection is configured")
            with session.begin_nested():
                adapter = adapter_factory(schedule.provider, company_id)
                apply_sync(session, schedule.provider, adapter, actor)
            schedule.last_success_at = now
            schedule.last_error = None
            schedule.next_run_at = now + timedelta(minutes=schedule.interval_minutes)
            summary["synced"] += 1
        except Exception as exc:
            schedule.last_error = str(exc)[:1000]
            schedule.next_run_at = now + timedelta(minutes=5)
            session.add(AccountingConnectionEventRecord(
                provider=schedule.provider, event_type="ACCOUNTING_MASTER_DATA_SYNC_FAILED",
                actor=actor, details={"error": schedule.last_error},
            ))
            summary["failed"] += 1
    session.commit()
    return summary
