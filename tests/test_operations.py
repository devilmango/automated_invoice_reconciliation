from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from invoice_match.database import ExceptionRecord, MatchRecord, SessionLocal

ROOT = Path(__file__).resolve().parents[1]


def request_fixture():
    return json.loads((ROOT / "examples" / "match-request.json").read_text())


def submit(client, headers, request, prefix):
    response = client.post(
        "/matches",
        json=request,
        headers={**headers, "Idempotency-Key": f"{prefix}-{uuid4().hex[:12]}"},
    )
    assert response.status_code == 201
    return response.json()


def test_admin_can_retry_failed_ap_export_and_reconcile_reference(
    client, integration_headers, reviewer_headers, admin_headers, monkeypatch
):
    request = request_fixture()
    request["receipt"]["items"][0]["qty"] = 100
    match = submit(client, integration_headers, request, "operations-match")
    match_id = match["match_id"]

    class FailingAdapter:
        name = "test-ap"

        def submit_invoice(self, payload, *, idempotency_key):
            assert idempotency_key == match_id
            raise RuntimeError("AP system temporarily unavailable")

    class SuccessfulAdapter:
        name = "test-ap"

        def submit_invoice(self, payload, *, idempotency_key):
            assert idempotency_key == match_id
            return "AP-BILL-44"

    monkeypatch.setattr("invoice_match.api.create_adapter", lambda _name: FailingAdapter())
    denied = client.post(f"/reviewer/ap-outbox/{match_id}/retry", headers=reviewer_headers)
    failed = client.post(f"/reviewer/ap-outbox/{match_id}/retry", headers=admin_headers)
    assert denied.status_code == 403
    assert failed.status_code == 200
    assert failed.json()["state"] == "PENDING"
    assert failed.json()["attempt_count"] == 1
    assert failed.json()["last_error"] == "AP system temporarily unavailable"

    monkeypatch.setattr("invoice_match.api.create_adapter", lambda _name: SuccessfulAdapter())
    retried = client.post(f"/reviewer/ap-outbox/{match_id}/retry", headers=admin_headers)
    assert retried.status_code == 200
    assert retried.json()["state"] == "ACKNOWLEDGED"
    assert retried.json()["external_reference"] == "AP-BILL-44"
    assert retried.json()["attempt_count"] == 2

    reconciled = client.post(
        f"/reviewer/ap-outbox/{match_id}/reconcile",
        headers=admin_headers,
        json={"external_reference": "BILL-2026-0044", "note": "Confirmed in AP."},
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["external_reference"] == "BILL-2026-0044"
    audit = client.get(f"/matches/{match_id}/audit", headers=reviewer_headers).json()
    event_types = [event["event_type"] for event in audit]
    assert "AP_EXPORT_RETRY_REQUESTED" in event_types
    assert "AP_EXPORT_FAILED" in event_types
    assert "AP_EXPORT_ACKNOWLEDGED" in event_types
    reconciliation = next(event for event in audit if event["event_type"] == "AP_EXPORT_REFERENCE_RECONCILED")
    assert reconciliation["details"]["note"] == "Confirmed in AP."


def test_operations_report_summarizes_matches_approvals_slas_and_exports(
    client, integration_headers, reviewer_headers
):
    open_exception = submit(client, integration_headers, request_fixture(), "report-open")
    decided_request = request_fixture()
    decided_request["po"]["number"] = "PO-REPORT-DECIDED"
    decided_request["invoice"]["number"] = "INV-REPORT-APPROVED"
    decided = submit(client, integration_headers, decided_request, "report-decided")
    matched_request = request_fixture()
    matched_request["po"]["number"] = "PO-REPORT-MATCHED"
    matched_request["invoice"]["number"] = "INV-REPORT-MATCHED"
    matched_request["receipt"]["items"][0]["qty"] = 100
    matched = submit(client, integration_headers, matched_request, "report-matched")

    approved = client.post(
        f"/exceptions/{decided['match_id']}/approve",
        headers=reviewer_headers,
        json={"comment": "Report test approval."},
    )
    assert approved.status_code == 200
    acknowledged = client.post(
        f"/integrations/ap/outbox/{matched['match_id']}/ack",
        headers=integration_headers,
        json={"external_reference": "AP-REPORT-1"},
    )
    assert acknowledged.status_code == 200

    with SessionLocal() as session:
        exception = session.scalar(
            select(ExceptionRecord).where(ExceptionRecord.match_id == open_exception["match_id"])
        )
        exception.escalated = True
        exception.escalated_to = "admin"
        match_record = session.get(MatchRecord, open_exception["match_id"])
        match_record.due_at = datetime.now(timezone.utc) - timedelta(hours=2)
        session.commit()

    today = datetime.now(timezone.utc).date().isoformat()
    report = client.get(
        f"/reviewer/reports/operations?start_date={today}&end_date={today}",
        headers=reviewer_headers,
    )
    assert report.status_code == 200
    payload = report.json()
    assert payload["matches"] == {"total": 3, "matched": 1, "exceptions": 2, "match_rate_percent": 33.3}
    assert payload["approvals"]["decisions"] == 1
    assert payload["approvals"]["average_turnaround_hours"] is not None
    assert payload["exceptions"]["open"] == 1
    assert payload["exceptions"]["overdue"] == 1
    assert payload["exceptions"]["escalated"] == 1
    assert payload["ap_exports"]["acknowledged"] == 1
    assert payload["ap_exports"]["pending"] == 1
    assert payload["by_vendor"][0]["matches"] == 3
