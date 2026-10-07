from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from invoice_match.database import MatchRecord, NotificationOutboxRecord, SessionLocal
from invoice_match.notification_delivery import (
    WebhookNotificationAdapter,
    deliver_pending_notifications,
    queue_due_reminders,
)

ROOT = Path(__file__).resolve().parents[1]


def submit_exception(client, integration_headers):
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())
    request["invoice"]["number"] = f"INV-{uuid4().hex[:12]}"
    response = client.post("/matches", json=request,
                           headers={**integration_headers, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 201
    return response.json()["match_id"]


def test_exception_creation_queues_durable_webhook(client, integration_headers, reviewer_headers):
    match_id = submit_exception(client, integration_headers)
    response = client.get("/notifications/outbox", headers=reviewer_headers)

    assert response.status_code == 200
    notification = response.json()[0]
    assert notification["match_id"] == match_id
    assert notification["event_type"] == "EXCEPTION_CREATED"
    assert notification["state"] == "PENDING"
    assert notification["payload"]["purchase_order_number"] == "PO-10291"


def test_due_reminders_are_daily_deduplicated_and_escalation_is_notified(
    client, integration_headers, reviewer_headers
):
    match_id = submit_exception(client, integration_headers)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        match = session.get(MatchRecord, match_id)
        match.due_at = now + timedelta(hours=2)
        session.commit()
        first = queue_due_reminders(session, now=now)
        second = queue_due_reminders(session, now=now + timedelta(minutes=5))
        assert len(first) == 1
        assert second == []
        match.due_at = now - timedelta(hours=1)
        session.commit()

    escalation = client.post("/exceptions/escalate-overdue", headers=reviewer_headers)
    assert escalation.status_code == 200
    outbox = client.get("/notifications/outbox", headers=reviewer_headers).json()
    events = [item["event_type"] for item in outbox if item["match_id"] == match_id]
    assert events.count("EXCEPTION_CREATED") == 1
    assert events.count("APPROVAL_DUE_SOON") == 1
    assert events.count("EXCEPTION_ESCALATED") == 1


def test_notification_delivery_retries_with_backoff_and_then_acknowledges(
    client, integration_headers
):
    match_id = submit_exception(client, integration_headers)
    start = datetime.now(timezone.utc)

    class FlakyWebhook:
        name = "webhook-test"
        calls = 0

        def deliver(self, payload, *, idempotency_key):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary downstream error")
            assert idempotency_key.startswith("EXCEPTION_CREATED:")
            return "delivery-123"

    adapter = FlakyWebhook()
    with SessionLocal() as session:
        first = deliver_pending_notifications(session, adapter, now=start, limit=1)
        record = session.query(NotificationOutboxRecord).filter_by(match_id=match_id).one()
        assert first["failed"] == 1
        assert record.attempt_count == 1
        assert record.next_attempt_at is not None
        retry_at = record.next_attempt_at.replace(tzinfo=timezone.utc)
        too_early = deliver_pending_notifications(session, adapter, now=start + timedelta(seconds=10))
        assert too_early["delivered"] == 0
        second = deliver_pending_notifications(session, adapter, now=retry_at)
        assert second["delivered"] == 1
        assert record.state == "DELIVERED"
        assert record.provider_reference == "delivery-123"
        assert record.last_error is None


def test_dead_letter_can_only_be_retried_by_admin(
    client, integration_headers, admin_headers, reviewer_headers
):
    match_id = submit_exception(client, integration_headers)

    class BrokenWebhook:
        name = "broken-webhook"

        def deliver(self, payload, *, idempotency_key):
            raise RuntimeError("webhook unavailable")

    with SessionLocal() as session:
        result = deliver_pending_notifications(
            session, BrokenWebhook(), limit=1, max_attempts=1,
        )
        record = session.query(NotificationOutboxRecord).filter_by(match_id=match_id).one()
        assert result["dead_lettered"] == 1
        notification_id = record.id

    denied = client.post(f"/notifications/outbox/{notification_id}/retry", headers=reviewer_headers)
    retried = client.post(f"/notifications/outbox/{notification_id}/retry", headers=admin_headers)
    assert denied.status_code == 403
    assert retried.status_code == 200
    assert retried.json()["state"] == "PENDING"
    assert retried.json()["attempt_count"] == 0


def test_webhook_adapter_requires_tls_outside_local_development():
    for endpoint in ("http://notifications.example.com/hook", "http://localhost.attacker.test/hook"):
        try:
            WebhookNotificationAdapter(endpoint)
        except ValueError as exc:
            assert "HTTPS" in str(exc)
        else:
            raise AssertionError("non-TLS webhook URL should be rejected")
    WebhookNotificationAdapter("http://localhost:8081/hook")
