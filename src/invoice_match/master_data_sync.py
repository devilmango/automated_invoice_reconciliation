from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    AccountingConnectionEventRecord,
    AccountingConnectionHealthRecord,
    AccountingConnectionRecord,
    MasterDataSyncRunRecord,
    MasterDataSyncScheduleRecord,
)
from .notification_delivery import queue_system_notification


def _admin_url(provider: str) -> str:
    base = os.getenv("INVOICE_MATCH_ADMIN_URL", "/reviewer/operations").rstrip("/")
    return f"{base}?provider={provider}&view=master-data-sync"


def provider_failure_type(exc: Exception) -> str:
    message = str(exc).casefold()
    if getattr(exc, "status", None) in {401, 403} or any(marker in message for marker in (
        "oauth", "token", "authentication", "unauthorized", "invalid_grant",
    )):
        return "AUTHENTICATION"
    return "CONNECTION"


def queue_master_data_alert(session: Session, *, provider: str, kind: str,
                            message: str, anchor: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    queue_system_notification(
        session, provider=provider, kind=kind,
        dedupe_key=f"{kind}:{provider}:{anchor}:{today}",
        payload={"event_type": kind, "provider": provider, "message": message,
                 "admin_url": _admin_url(provider), "occurred_at": datetime.now(timezone.utc).isoformat()},
    )


def execute_master_data_sync(session: Session, *, provider: str, company_id: str,
                             adapter_factory, apply_sync, actor: str,
                             trigger_type: str) -> MasterDataSyncRunRecord:
    """Persist a provider sync attempt and its resulting diff or failure."""
    started_at = datetime.now(timezone.utc)
    started_clock = time.perf_counter()
    run = MasterDataSyncRunRecord(id=str(uuid4()), provider=provider,
        trigger_type=trigger_type, actor=actor, status="RUNNING", counts={}, changes={},
        started_at=started_at)
    schedule = session.scalar(select(MasterDataSyncScheduleRecord).where(
        MasterDataSyncScheduleRecord.provider == provider))
    if schedule is None:
        schedule = MasterDataSyncScheduleRecord(id=str(uuid4()), provider=provider,
            enabled=False, interval_minutes=1440, stale_after_minutes=2880)
        session.add(schedule)
    schedule.last_started_at = started_at
    session.add(run)
    session.commit()

    try:
        with session.begin_nested():
            connection = session.scalar(select(AccountingConnectionRecord).where(
                AccountingConnectionRecord.provider == provider,
                AccountingConnectionRecord.enabled.is_(True)))
            if connection is None:
                raise ValueError("No enabled accounting connection is configured")
            adapter = adapter_factory(provider, company_id)
            counts, changes = apply_sync(session, provider, adapter, actor)
        finished_at = datetime.now(timezone.utc)
        run.status = "SUCCEEDED"
        run.counts = counts
        run.changes = changes
        run.duration_ms = max(0, int((time.perf_counter() - started_clock) * 1000))
        run.finished_at = finished_at
        run.error = None
        if schedule is not None:
            schedule.last_success_at = finished_at
            schedule.last_error = None
            schedule.next_run_at = (finished_at + timedelta(minutes=schedule.interval_minutes)
                                    if schedule.enabled else None)
        session.commit()
        return run
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        run.status = "FAILED"
        run.duration_ms = max(0, int((time.perf_counter() - started_clock) * 1000))
        run.finished_at = finished_at
        run.error = str(exc)[:1000]
        if schedule is not None:
            schedule.last_error = run.error
            schedule.next_run_at = finished_at + timedelta(minutes=5) if schedule.enabled else None
        failure_type = provider_failure_type(exc)
        if failure_type == "AUTHENTICATION":
            health = session.scalar(select(AccountingConnectionHealthRecord).where(
                AccountingConnectionHealthRecord.provider == provider))
            if health is None:
                health = AccountingConnectionHealthRecord(id=str(uuid4()), provider=provider,
                    status="FAILED", checked_at=finished_at)
                session.add(health)
            health.status, health.failure_type = "FAILED", failure_type
            health.error, health.checked_at = run.error, finished_at
        session.add(AccountingConnectionEventRecord(provider=provider,
            event_type="ACCOUNTING_MASTER_DATA_SYNC_FAILED", actor=actor,
            details={"sync_run_id": run.id, "error": run.error}))
        queue_master_data_alert(session, provider=provider, kind="MASTER_DATA_SYNC_FAILED",
            message=run.error, anchor="daily")
        session.commit()
        raise


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
        try:
            execute_master_data_sync(session, provider=schedule.provider, company_id=company_id,
                adapter_factory=adapter_factory, apply_sync=apply_sync, actor=actor,
                trigger_type="SCHEDULED")
            summary["synced"] += 1
        except Exception:
            summary["failed"] += 1
    if queue_stale_master_data_alerts(session):
        session.commit()
    return summary


def queue_stale_master_data_alerts(session: Session, *, admin_url: str | None = None) -> int:
    """Queue one daily webhook alert for each provider whose references are stale."""
    now = datetime.now(timezone.utc)
    schedules = session.scalars(select(MasterDataSyncScheduleRecord)).all()
    queued = 0
    for schedule in schedules:
        last_success = schedule.last_success_at
        if last_success is not None and last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        if schedule.last_error:
            continue
        if not schedule.enabled and last_success is None:
            continue
        if last_success is not None and now - last_success <= timedelta(minutes=schedule.stale_after_minutes):
            continue
        anchor = last_success.isoformat() if last_success else "never-synced"
        message = ("Master data has never synced successfully" if last_success is None
                   else "Master data has passed its configured freshness window")
        notification = queue_system_notification(session, provider=schedule.provider,
            kind="MASTER_DATA_SYNC_STALE",
            dedupe_key=f"MASTER_DATA_SYNC_STALE:{schedule.provider}:{anchor}:{now.date().isoformat()}",
            payload={"event_type": "MASTER_DATA_SYNC_STALE", "provider": schedule.provider,
                     "message": message,
                     "admin_url": admin_url or _admin_url(schedule.provider),
                     "occurred_at": now.isoformat()})
        if notification is not None:
            queued += 1
    return queued
