from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    APOutboxRecord,
    AccountingPeriodCloseRecord,
    CreditApplicationOutboxRecord,
    CreditNoteApplicationRecord,
    ExceptionRecord,
    MatchRecord,
    MasterDataSyncRunRecord,
)


def match_accounting_date(match: MatchRecord) -> date:
    invoice = (match.input_data or {}).get("invoice", {})
    raw_date = invoice.get("invoice_date")
    if raw_date:
        try:
            return date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            pass
    created_at = match.created_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc).date()


def build_period_close_report(session: Session, period_start: date, period_end: date) -> dict:
    """Return the live close checklist and evidence for an inclusive invoice-date range."""
    if period_end < period_start:
        raise ValueError("period_end must be on or after period_start")
    matches = session.scalars(select(MatchRecord)).all()
    scoped_matches = {match.id: match for match in matches
                      if period_start <= match_accounting_date(match) <= period_end}

    exceptions = []
    for exception, match in session.execute(select(ExceptionRecord, MatchRecord).join(
            MatchRecord, MatchRecord.id == ExceptionRecord.match_id).where(
                ExceptionRecord.queue_status == "OPEN",
                ExceptionRecord.match_id.in_(scoped_matches.keys() or {"__none__"}),
            )).all():
        exceptions.append({"match_id": match.id, "invoice_number": match.invoice_number,
            "vendor": match.vendor, "po_number": match.po_number, "reason": match.reason,
            "created_at": match.created_at.isoformat() if match.created_at else None,
            "discrepancies": exception.discrepancies})

    pending_ap_exports = []
    for outbox, match in session.execute(select(APOutboxRecord, MatchRecord).join(
            MatchRecord, MatchRecord.id == APOutboxRecord.match_id).where(
                APOutboxRecord.state != "ACKNOWLEDGED",
                APOutboxRecord.match_id.in_(scoped_matches.keys() or {"__none__"}),
            )).all():
        pending_ap_exports.append({"match_id": match.id, "invoice_number": match.invoice_number,
            "state": outbox.state, "attempt_count": outbox.attempt_count,
            "last_error": outbox.last_error})

    pending_credit_syncs = []
    for outbox, application, credit_match in session.execute(
        select(CreditApplicationOutboxRecord, CreditNoteApplicationRecord, MatchRecord)
        .join(CreditNoteApplicationRecord,
              CreditNoteApplicationRecord.id == CreditApplicationOutboxRecord.application_id)
        .join(MatchRecord, MatchRecord.id == CreditNoteApplicationRecord.credit_match_id)
        .where(CreditApplicationOutboxRecord.state != "ACKNOWLEDGED")
    ).all():
        if period_start <= match_accounting_date(credit_match) <= period_end:
            pending_credit_syncs.append({"outbox_id": outbox.id,
                "credit_match_id": application.credit_match_id,
                "invoice_match_id": application.invoice_match_id,
                "credit_note_number": credit_match.invoice_number,
                "state": outbox.state, "attempt_count": outbox.attempt_count,
                "last_error": outbox.last_error})

    period_start_at = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    period_end_exclusive = datetime.combine(period_end + timedelta(days=1),
                                            datetime.min.time(), tzinfo=timezone.utc)
    provider_syncs = session.scalars(select(MasterDataSyncRunRecord).where(
        MasterDataSyncRunRecord.started_at >= period_start_at,
        MasterDataSyncRunRecord.started_at < period_end_exclusive,
        MasterDataSyncRunRecord.status.in_(("RUNNING", "FAILED")),
    ).order_by(MasterDataSyncRunRecord.started_at)).all()
    unresolved_provider_syncs = [{"run_id": run.id, "provider": run.provider,
        "status": run.status, "started_at": run.started_at.isoformat(), "error": run.error}
        for run in provider_syncs]

    blockers = {
        "open_exceptions": len(exceptions),
        "pending_ap_exports": len(pending_ap_exports),
        "pending_credit_syncs": len(pending_credit_syncs),
        "failed_or_running_provider_syncs": len(unresolved_provider_syncs),
    }
    return {
        "period": {"start_date": period_start.isoformat(), "end_date": period_end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "blocker_count": sum(blockers.values()),
        "ready_to_close": not any(blockers.values()),
        "blockers": blockers,
        "unresolved_exceptions": exceptions,
        "pending_ap_exports": pending_ap_exports,
        "pending_credit_syncs": pending_credit_syncs,
        "unresolved_provider_syncs": unresolved_provider_syncs,
    }


def locked_period_for_date(session: Session, accounting_date: date) -> AccountingPeriodCloseRecord | None:
    return session.scalar(select(AccountingPeriodCloseRecord).where(
        AccountingPeriodCloseRecord.status == "LOCKED",
        AccountingPeriodCloseRecord.period_start <= accounting_date,
        AccountingPeriodCloseRecord.period_end >= accounting_date,
    ).order_by(AccountingPeriodCloseRecord.period_start.desc()).limit(1))
