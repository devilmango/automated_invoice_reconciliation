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
    NotificationOutboxRecord,
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
                            message: str, anchor: str, details: dict | None = None):
    today = datetime.now(timezone.utc).date().isoformat()
    payload = {"event_type": kind, "provider": provider, "message": message,
               "admin_url": _admin_url(provider), "occurred_at": datetime.now(timezone.utc).isoformat()}
    payload.update(details or {})
    return queue_system_notification(
        session, provider=provider, kind=kind,
        dedupe_key=f"{kind}:{provider}:{anchor}:{today}",
        payload=payload,
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
    previous_sync_error = schedule.last_error if schedule is not None else None
    if schedule is None:
        schedule = MasterDataSyncScheduleRecord(id=str(uuid4()), provider=provider,
            enabled=False, interval_minutes=1440, stale_after_minutes=2880,
            connection_check_enabled=True, connection_check_interval_minutes=60,
            next_connection_check_at=started_at)
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
            applied = apply_sync(session, provider, adapter, actor)
            counts, changes = applied[:2]
            details = applied[2] if len(applied) > 2 else {}
        finished_at = datetime.now(timezone.utc)
        run.status = "SUCCEEDED"
        run.counts = counts
        run.changes = changes
        run.details = details
        run.duration_ms = max(0, int((time.perf_counter() - started_clock) * 1000))
        run.finished_at = finished_at
        run.error = None
        if previous_sync_error:
            health = session.scalar(select(AccountingConnectionHealthRecord).where(
                AccountingConnectionHealthRecord.provider == provider))
            if health is not None:
                health.status, health.failure_type, health.error = "HEALTHY", None, None
                health.checked_at = finished_at
        if schedule is not None:
            schedule.last_success_at = finished_at
            schedule.last_error = None
            schedule.next_run_at = (finished_at + timedelta(minutes=schedule.interval_minutes)
                                    if schedule.enabled else None)
        if previous_sync_error:
            failed_run = session.scalar(select(MasterDataSyncRunRecord).where(
                MasterDataSyncRunRecord.provider == provider,
                MasterDataSyncRunRecord.status == "FAILED",
            ).order_by(MasterDataSyncRunRecord.started_at.desc()).limit(1))
            failed_run_id = failed_run.id if failed_run else "previous-failure"
            recovery_key = f"MASTER_DATA_SYNC_RECOVERED:{provider}:{failed_run_id}"
            recovered_payload = {"event_type": "MASTER_DATA_SYNC_RECOVERED", "provider": provider,
                "message": "Provider master-data sync recovered after a previous failure.",
                "previous_error": previous_sync_error, "failed_run_id": failed_run_id,
                "recovery_run_id": run.id, "admin_url": _admin_url(provider),
                "occurred_at": finished_at.isoformat()}
            recovery_alert = queue_system_notification(session, provider=provider, kind="MASTER_DATA_SYNC_RECOVERED",
                dedupe_key=recovery_key, payload=recovered_payload)
            failed_alerts = session.scalars(select(NotificationOutboxRecord).where(
                NotificationOutboxRecord.provider == provider,
                NotificationOutboxRecord.event_type == "MASTER_DATA_SYNC_FAILED",
                NotificationOutboxRecord.resolved_at.is_(None),
            ).order_by(NotificationOutboxRecord.created_at.desc())).all()
            failed_alert = next((alert for alert in failed_alerts
                if alert.payload.get("failed_run_id") == failed_run_id), None)
            if failed_alert is None:
                failed_alert = next(iter(failed_alerts), None)
            if failed_alert is not None:
                failed_alert.resolved_at = finished_at
                failed_alert.resolved_by_alert_id = recovery_alert.id if recovery_alert else None
                if recovery_alert is not None:
                    recovery_alert.resolves_alert_id = failed_alert.id
                recovered_payload["resolved_alert_id"] = failed_alert.id
                if recovery_alert is not None:
                    recovery_alert.payload = recovered_payload
            session.add(AccountingConnectionEventRecord(provider=provider,
                event_type="ACCOUNTING_MASTER_DATA_SYNC_RECOVERED", actor=actor,
                details={"failed_run_id": failed_run_id, "recovery_run_id": run.id,
                         "previous_error": previous_sync_error}))
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
            message=run.error, anchor=run.id, details={"failed_run_id": run.id})
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
    queued = queue_stale_master_data_alerts(session)
    escalated = escalate_unacknowledged_master_data_alerts(session)
    if queued or escalated:
        session.commit()
    return summary


def check_due_provider_connections(session: Session, *, company_id: str, adapter_factory,
                                   actor: str, limit: int = 100) -> dict[str, int]:
    """Run scheduled provider authentication/connection checks and persist each outcome."""
    now = datetime.now(timezone.utc)
    enabled_providers = session.scalars(select(AccountingConnectionRecord.provider).where(
        AccountingConnectionRecord.enabled.is_(True))).all()
    for provider in enabled_providers:
        schedule = session.scalar(select(MasterDataSyncScheduleRecord).where(
            MasterDataSyncScheduleRecord.provider == provider))
        if schedule is None:
            session.add(MasterDataSyncScheduleRecord(id=str(uuid4()), provider=provider,
                enabled=False, interval_minutes=1440, stale_after_minutes=2880,
                connection_check_enabled=True, connection_check_interval_minutes=60,
                next_connection_check_at=now))
    session.flush()
    due = session.scalars(select(MasterDataSyncScheduleRecord).where(
        MasterDataSyncScheduleRecord.connection_check_enabled.is_(True),
        MasterDataSyncScheduleRecord.next_connection_check_at <= now,
    ).order_by(MasterDataSyncScheduleRecord.next_connection_check_at).limit(limit)).all()
    summary = {"checked": 0, "failed": 0}
    for schedule in due:
        checked_at = datetime.now(timezone.utc)
        error, failure_type, account = None, None, {}
        try:
            connection = session.scalar(select(AccountingConnectionRecord).where(
                AccountingConnectionRecord.provider == schedule.provider,
                AccountingConnectionRecord.enabled.is_(True)))
            if connection is None:
                raise ValueError("No enabled accounting connection is configured")
            adapter = adapter_factory(schedule.provider, company_id)
            check = getattr(adapter, "test_connection", None)
            if not callable(check):
                raise RuntimeError(f"The {schedule.provider} adapter does not support connection checks")
            account = check() or {}
            status = "HEALTHY"
        except Exception as exc:
            status = "FAILED"
            error = str(exc)[:1000]
            failure_type = provider_failure_type(exc)
        health = session.scalar(select(AccountingConnectionHealthRecord).where(
            AccountingConnectionHealthRecord.provider == schedule.provider))
        if health is None:
            health = AccountingConnectionHealthRecord(id=str(uuid4()), provider=schedule.provider,
                status=status, checked_at=checked_at)
            session.add(health)
        health.status, health.failure_type = status, failure_type
        health.account_name = str(account.get("account_name"))[:255] if account.get("account_name") else None
        health.account_id = str(account.get("account_id"))[:160] if account.get("account_id") else None
        health.error, health.checked_at = error, checked_at
        schedule.last_connection_check_at = checked_at
        schedule.next_connection_check_at = checked_at + timedelta(
            minutes=schedule.connection_check_interval_minutes)
        event = "ACCOUNTING_CONNECTION_HEALTH_CHECKED" if status == "HEALTHY" else "ACCOUNTING_CONNECTION_HEALTH_CHECK_FAILED"
        session.add(AccountingConnectionEventRecord(provider=schedule.provider,
            event_type=event, actor=actor,
            details={"status": status, "failure_type": failure_type, "error": error,
                     "account_id": health.account_id, "checked_at": checked_at.isoformat()}))
        if status == "FAILED":
            summary["failed"] += 1
        else:
            summary["checked"] += 1
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


def escalate_unacknowledged_master_data_alerts(session: Session) -> int:
    """Queue a single escalation notice after an alert's configured response window."""
    now = datetime.now(timezone.utc)
    schedules = {row.provider: row for row in session.scalars(
        select(MasterDataSyncScheduleRecord)).all()}
    alerts = session.scalars(select(NotificationOutboxRecord).where(
        NotificationOutboxRecord.provider.is_not(None),
        NotificationOutboxRecord.event_type.in_(
            ("MASTER_DATA_SYNC_FAILED", "MASTER_DATA_SYNC_STALE", "MASTER_DATA_SYNC_RECOVERED")),
        NotificationOutboxRecord.acknowledged_at.is_(None),
        NotificationOutboxRecord.resolved_at.is_(None),
        NotificationOutboxRecord.escalated_at.is_(None),
    )).all()
    count = 0
    for alert in alerts:
        schedule = schedules.get(alert.provider)
        if schedule is None or not schedule.alert_escalation_enabled:
            continue
        created_at = alert.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        snoozed_until = alert.snoozed_until
        if snoozed_until is not None:
            if snoozed_until.tzinfo is None:
                snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
            if snoozed_until > now:
                continue
        if now - created_at < timedelta(minutes=schedule.alert_escalation_minutes):
            continue
        notification = queue_system_notification(session, provider=alert.provider,
            kind="MASTER_DATA_SYNC_ESCALATED",
            dedupe_key=f"MASTER_DATA_SYNC_ESCALATED:{alert.id}",
            payload={"event_type": "MASTER_DATA_SYNC_ESCALATED", "provider": alert.provider,
                "message": "A provider alert remains unacknowledged beyond its escalation window.",
                "source_alert_id": alert.id, "source_event_type": alert.event_type,
                "admin_url": _admin_url(alert.provider), "occurred_at": now.isoformat()})
        alert.escalated_at = now
        session.add(AccountingConnectionEventRecord(provider=alert.provider,
            event_type="ADMIN_ALERT_ESCALATED", actor="system",
            details={"notification_id": alert.id, "escalation_notification_id": notification.id if notification else None,
                     "event_type": alert.event_type, "escalated_at": now.isoformat()}))
        count += 1
    return count
