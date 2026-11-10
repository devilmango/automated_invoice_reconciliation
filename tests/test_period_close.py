from __future__ import annotations

import json
from pathlib import Path


def match_request(invoice_number: str, *, invoice_date: str = "2026-10-15", receipt_qty: int = 5):
    request = json.loads((Path(__file__).parent.parent / "examples" / "match-request.json").read_text())
    request["po"]["number"] = f"PO-{invoice_number}"
    request["po"]["items"][0].update(qty=5, price=10)
    request["receipt"]["number"] = f"GR-{invoice_number}"
    request["receipt"]["items"][0]["qty"] = receipt_qty
    request["invoice"].update(number=invoice_number, invoice_date=invoice_date)
    request["invoice"]["items"][0].update(qty=5, price=10)
    return request


def submit(client, request, key):
    return client.post("/matches", json=request,
        headers={"Authorization": "Bearer test-integration-secret-token-for-unit-tests-0001",
                  "Idempotency-Key": key})


def test_period_close_reports_open_exceptions_as_blockers(client, admin_headers):
    response = submit(client, match_request("INV-CLOSE-EXCEPTION", receipt_qty=4), "close-exception")
    assert response.status_code == 201

    preview = client.get("/admin/period-closes/preview", params={
        "period_start": "2026-10-01", "period_end": "2026-10-31",
    }, headers=admin_headers)
    assert preview.status_code == 200
    report = preview.json()
    assert report["ready_to_close"] is False
    assert report["blockers"]["open_exceptions"] == 1
    assert report["unresolved_exceptions"][0]["invoice_number"] == "INV-CLOSE-EXCEPTION"

    close = client.post("/admin/period-closes", json={
        "period_start": "2026-10-01", "period_end": "2026-10-31", "signoff_note": "October close",
    }, headers=admin_headers)
    assert close.status_code == 409
    assert close.json()["detail"]["code"] == "PERIOD_CLOSE_BLOCKED"


def test_period_close_requires_clear_exports_signs_off_and_locks_matching(client, admin_headers,
                                                                           integration_headers):
    request = match_request("INV-CLOSE-CLEAR")
    response = submit(client, request, "close-clear")
    assert response.status_code == 201
    match_id = response.json()["match_id"]

    preview = client.get("/admin/period-closes/preview", params={
        "period_start": "2026-10-01", "period_end": "2026-10-31",
    }, headers=admin_headers)
    assert preview.status_code == 200
    assert preview.json()["blockers"]["pending_ap_exports"] == 1
    assert preview.json()["pending_ap_exports"][0]["match_id"] == match_id

    blocked = client.post("/admin/period-closes", json={
        "period_start": "2026-10-01", "period_end": "2026-10-31", "signoff_note": "October close",
    }, headers=admin_headers)
    assert blocked.status_code == 409

    acknowledged = client.post(f"/integrations/ap/outbox/{match_id}/ack",
        json={"external_reference": "bill-123"}, headers=integration_headers)
    assert acknowledged.status_code == 200

    closed = client.post("/admin/period-closes", json={
        "period_start": "2026-10-01", "period_end": "2026-10-31", "signoff_note": "Reviewed and reconciled",
    }, headers=admin_headers)
    assert closed.status_code == 201
    close_id = closed.json()["id"]
    assert closed.json()["status"] == "LOCKED"
    assert closed.json()["signed_off_by"] == "cfo"
    assert closed.json()["report_snapshot"]["ready_to_close"] is True

    locked = submit(client, match_request("INV-AFTER-CLOSE"), "after-close")
    assert locked.status_code == 423
    assert locked.json()["detail"]["code"] == "ACCOUNTING_PERIOD_LOCKED"

    reopened = client.post(f"/admin/period-closes/{close_id}/reopen",
        json={"note": "Late supplier adjustment"}, headers=admin_headers)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "REOPENED"
    details = client.get(f"/admin/period-closes/{close_id}", headers=admin_headers)
    assert details.status_code == 200
    assert [event["event_type"] for event in details.json()["history"]] == [
        "PERIOD_LOCKED", "PERIOD_REOPENED"]
    assert details.json()["history"][-1]["details"]["note"] == "Late supplier adjustment"

    unlocked = submit(client, match_request("INV-AFTER-REOPEN"), "after-reopen")
    assert unlocked.status_code != 423
