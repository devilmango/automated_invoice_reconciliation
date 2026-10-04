from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def sample_request():
    return json.loads((ROOT / "examples" / "match-request.json").read_text())


def match_headers(integration_headers, key=None):
    return {**integration_headers, "Idempotency-Key": key or str(uuid4())}


def test_idempotency_key_replays_original_match(client, integration_headers):
    request = sample_request()
    headers = match_headers(integration_headers, "invoice-10291-request-0001")

    first = client.post("/matches", json=request, headers=headers)
    replay = client.post("/matches", json=request, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["match_id"] == first.json()["match_id"]


def test_idempotency_key_cannot_be_reused_for_different_content(client, integration_headers):
    headers = match_headers(integration_headers, "invoice-10291-request-0002")
    first = client.post("/matches", json=sample_request(), headers=headers)
    changed = sample_request()
    changed["po"]["number"] = "PO-OTHER"

    response = client.post("/matches", json=changed, headers=headers)

    assert first.status_code == 201
    assert response.status_code == 409


def test_duplicate_supplier_invoice_is_rejected_even_with_new_key(client, integration_headers):
    first = client.post(
        "/matches",
        json=sample_request(),
        headers=match_headers(integration_headers),
    )
    duplicate = sample_request()
    duplicate["po"]["number"] = "PO-10292"

    response = client.post(
        "/matches",
        json=duplicate,
        headers=match_headers(integration_headers),
    )

    assert first.status_code == 201
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DUPLICATE_SUPPLIER_INVOICE"
    assert response.json()["detail"]["existing_match_id"] == first.json()["match_id"]


def test_match_submission_requires_an_idempotency_key(client, integration_headers):
    response = client.post(
        "/matches",
        json=sample_request(),
        headers=integration_headers,
    )

    assert response.status_code == 422


def test_csv_import_creates_a_match(client, integration_headers):
    csv_documents = {
        "po_csv": (
            "number,vendor,currency,cost_center,sku,qty,price,tax_rate,tax_code,"
            "discount_rate,freight_amount,discount_amount\n"
            "PO-CSV-1,ABC Supplies,USD,CC-10,A100,100,10,0.08,STANDARD,0,0,0\n"
        ),
        "receipt_csv": "number,sku,qty\nGR-CSV-1,A100,100\n",
        "invoice_csv": (
            "number,vendor,currency,sku,qty,price,discount_rate,tax_code,tax_rate,"
            "tax_amount,freight_amount,discount_amount,total_amount\n"
            "INV-CSV-1,ABC Supplies,USD,A100,100,10,0,STANDARD,0.08,80,0,0,1080\n"
        ),
    }

    response = client.post(
        "/imports/csv",
        json=csv_documents,
        headers=match_headers(integration_headers),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "MATCHED"


def test_approved_payable_is_available_in_ap_outbox_and_can_be_acknowledged(
    client,
    integration_headers,
    reviewer_headers,
):
    request = sample_request()
    request["receipt"]["items"][0]["qty"] = 100
    match = client.post(
        "/matches",
        json=request,
        headers=match_headers(integration_headers),
    ).json()

    outbox = client.get("/integrations/ap/outbox", headers=integration_headers)
    assert outbox.status_code == 200
    assert outbox.json()[0]["match_id"] == match["match_id"]
    payload = outbox.json()[0]["payload"]
    assert payload["supplier_invoice_number"] == "INV-2044"
    assert payload["total_amount"] == "1000.00"

    acknowledged = client.post(
        f"/integrations/ap/outbox/{match['match_id']}/ack",
        headers=integration_headers,
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["state"] == "ACKNOWLEDGED"
    assert client.get("/integrations/ap/outbox", headers=integration_headers).json() == []
    assert client.get("/integrations/ap/outbox", headers=reviewer_headers).status_code == 401


def test_amount_policy_requires_distinct_approvers_and_records_assignment(
    client,
    integration_headers,
    reviewer_headers,
    admin_headers,
    tmp_path,
    monkeypatch,
):
    rules = tmp_path / "approval-rules.yaml"
    rules.write_text(
        """
rules:
  currency:
    base_currency: USD
    rates_to_base:
      USD: 1
  approval:
    policies:
      - name: high-value-two-step
        min_invoice_total: 500
        required_role: approver
        approvals_required: 2
        assigned_to: finance-manager
        sla_hours: 24
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("INVOICE_MATCH_RULES", str(rules))
    match_response = client.post(
        "/matches",
        json=sample_request(),
        headers=match_headers(integration_headers),
    )
    match = match_response.json()
    match_id = match["match_id"]
    queued = client.get("/exceptions", headers=reviewer_headers).json()[0]

    assert match_response.status_code == 201
    assert queued["approval_policy"] == "high-value-two-step"
    assert queued["assigned_to"] == "finance-manager"
    assert queued["approvals_required"] == 2
    assert queued["due_at"] is not None

    viewer_cannot_approve = client.post(
        f"/exceptions/{match_id}/approve",
        headers={**reviewer_headers, "Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0001"},
        json={},
    )
    first = client.post(
        f"/exceptions/{match_id}/approve",
        headers=reviewer_headers,
        json={"comment": "First review."},
    )
    repeat = client.post(
        f"/exceptions/{match_id}/approve",
        headers=reviewer_headers,
        json={"comment": "Second vote from same reviewer."},
    )
    second = client.post(
        f"/exceptions/{match_id}/approve",
        headers=admin_headers,
        json={"comment": "Second reviewer approval."},
    )

    assert viewer_cannot_approve.status_code == 403
    assert first.json()["approval_status"] == "PENDING"
    assert first.json()["approvals_received"] == 1
    assert repeat.status_code == 409
    assert second.json()["approval_status"] == "APPROVED"
    assert second.json()["approvals_received"] == 2
