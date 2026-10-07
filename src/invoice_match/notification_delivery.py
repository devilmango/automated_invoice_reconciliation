from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import (
    AuditEvent,
    ExceptionRecord,
    MatchRecord,
    NotificationOutboxRecord,
)

MAX_NOTIFICATION_ATTEMPTS = 8


class WebhookNotificationAdapter:
    """Deliver notification events to an HTTPS webhook with idempotency and optional HMAC."""

    name = "webhook"

    def __init__(self, endpoint: str, *, token: str | None = None, secret: str | None = None,
                 timeout: float = 10.0):
        parsed = urlsplit(endpoint)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Notification webhook must use HTTPS (HTTP is allowed for localhost)")
        self.endpoint = endpoint
        self.token = token
        self.secret = secret
        self.timeout = timeout

    def deliver(self, payload: dict, *, idempotency_key: str) -> str | None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Idempotency-Key": idempotency_key}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.secret:
            headers["X-Invoice-Match-Signature"] = hmac.new(
                self.secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16_384)
                reference = response.headers.get("X-Delivery-Id")
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(f"Notification webhook returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Notification webhook request failed: {exc.reason}") from exc
        if not reference and raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    reference = data.get("id") or data.get("reference")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return str(reference)[:255] if reference else None


def _notification_payload(kind: str, match: MatchRecord, *, escalated_to: str | None = None) -> dict:
    return {
        "event_type": kind,
        "match_id": match.id,
        "vendor": match.vendor,
        "purchase_order_number": match.po_number,
        "supplier_invoice_number": match.invoice_number,
        "approval_status": match.approval_status,
        "approval_policy": match.approval_policy,
        "assigned_to": match.assigned_to,
        "required_role": match.required_role,
        "due_at": match.due_at.isoformat() if match.due_at else None,
        "escalated_to": escalated_to,
        "reason": match.reason,
        "discrepancies": match.result_data.get("discrepancies", []),
    }


def queue_notification(
    session: Session,
    match: MatchRecord,
    kind: str,
    *,
    dedupe_suffix: str = "",
    escalated_to: str | None = None,
) -> NotificationOutboxRecord | None:
    dedupe_key = f"{kind}:{match.id}:{dedupe_suffix}"[:255]
    existing = session.scalar(select(NotificationOutboxRecord).where(
        NotificationOutboxRecord.dedupe_key == dedupe_key
    ))
    if existing is not None:
        return None
    notification = NotificationOutboxRecord(
        id=str(uuid4()),
        match_id=match.id,
        dedupe_key=dedupe_key,
        event_type=kind,
        channel="webhook",
        payload=_notification_payload(kind, match, escalated_to=escalated_to),
        state="PENDING",
        attempt_count=0,
    )
    session.add(notification)
    return notification


def queue_due_reminders(
    session: Session,
    *,
    window_hours: int = 24,
    now: datetime | None = None,
) -> list[str]:
    if not 1 <= window_hours <= 720:
        raise ValueError("window_hours must be between 1 and 720")
    current = now or datetime.now(timezone.utc)
    rows = session.execute(select(ExceptionRecord, MatchRecord).join(
        MatchRecord, ExceptionRecord.match_id == MatchRecord.id
    ).where(
        ExceptionRecord.queue_status == "OPEN",
        MatchRecord.due_at.is_not(None),
    )).all()
    today = current.astimezone(timezone.utc).date().isoformat()
    queued = []
    for exception, match in rows:
        due_at = match.due_at.replace(tzinfo=timezone.utc) if match.due_at.tzinfo is None else match.due_at
        if due_at > current + timedelta(hours=window_hours):
            continue
        event_type = "APPROVAL_OVERDUE" if due_at <= current else "APPROVAL_DUE_SOON"
        notification = queue_notification(
            session, match, event_type, dedupe_suffix=today,
            escalated_to=exception.escalated_to,
        )
        if notification is not None:
            queued.append(notification.id)
    return queued


def escalate_overdue(session: Session, *, actor: str, now: datetime | None = None) -> list[str]:
    current = now or datetime.now(timezone.utc)
    rows = session.execute(select(ExceptionRecord, MatchRecord).join(
        MatchRecord, ExceptionRecord.match_id == MatchRecord.id
    ).where(
        ExceptionRecord.queue_status == "OPEN",
        ExceptionRecord.escalated.is_(False),
        MatchRecord.due_at.is_not(None),
    )).all()
    escalated = []
    for exception, match in rows:
        due_at = match.due_at.replace(tzinfo=timezone.utc) if match.due_at.tzinfo is None else match.due_at
        if due_at > current:
            continue
        match.required_role = match.escalation_role
        match.assigned_to = None
        exception.escalated = True
        exception.escalated_to = match.escalation_role
        exception.escalated_at = current
        session.add(AuditEvent(
            match_id=match.id,
            event_type="EXCEPTION_ESCALATED",
            actor=actor,
            details={"escalated_to": match.escalation_role, "due_at": due_at.isoformat()},
        ))
        queue_notification(
            session, match, "EXCEPTION_ESCALATED", escalated_to=match.escalation_role,
        )
        escalated.append(match.id)
    return escalated


def deliver_pending_notifications(
    session: Session,
    adapter: WebhookNotificationAdapter,
    *,
    limit: int = 100,
    now: datetime | None = None,
    max_attempts: int = MAX_NOTIFICATION_ATTEMPTS,
) -> dict:
    current = now or datetime.now(timezone.utc)
    summary = {"adapter": adapter.name, "delivered": 0, "failed": 0, "dead_lettered": 0, "items": []}
    while len(summary["items"]) < limit:
        query = select(NotificationOutboxRecord).where(
            NotificationOutboxRecord.state == "PENDING",
            (NotificationOutboxRecord.next_attempt_at.is_(None)
             | (NotificationOutboxRecord.next_attempt_at <= current)),
        ).order_by(NotificationOutboxRecord.created_at.asc()).limit(1).with_for_update(skip_locked=True)
        record = session.scalar(query)
        if record is None:
            break
        record.attempt_count += 1
        try:
            reference = adapter.deliver(record.payload, idempotency_key=record.dedupe_key)
        except Exception as exc:  # Persist downstream failures so the scheduled worker can retry.
            record.last_error = str(exc)[:1000]
            if record.attempt_count >= max_attempts:
                record.state = "DEAD"
                summary["dead_lettered"] += 1
            else:
                delay_seconds = min(3600, 30 * (2 ** (record.attempt_count - 1)))
                record.next_attempt_at = current + timedelta(seconds=delay_seconds)
                summary["failed"] += 1
            session.add(AuditEvent(
                match_id=record.match_id,
                event_type="NOTIFICATION_DELIVERY_FAILED",
                actor=f"notification:{adapter.name}",
                details={"notification_id": record.id, "event_type": record.event_type,
                         "attempt": record.attempt_count, "state": record.state,
                         "error": record.last_error},
            ))
            summary["items"].append({"notification_id": record.id, "state": record.state,
                                     "error": record.last_error})
        else:
            record.state = "DELIVERED"
            record.provider_reference = reference
            record.delivered_at = current
            record.next_attempt_at = None
            record.last_error = None
            session.add(AuditEvent(
                match_id=record.match_id,
                event_type="NOTIFICATION_DELIVERED",
                actor=f"notification:{adapter.name}",
                details={"notification_id": record.id, "event_type": record.event_type,
                         "provider_reference": reference},
            ))
            summary["delivered"] += 1
            summary["items"].append({"notification_id": record.id, "state": "DELIVERED",
                                     "provider_reference": reference})
        session.commit()
    return summary
