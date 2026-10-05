from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from invoice_match.database import MatchRecord, SessionLocal

ROOT = Path(__file__).resolve().parents[1]


def submit_exception(client, integration_headers):
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())
    request["invoice"]["number"] = f"INV-{uuid4().hex[:12]}"
    response = client.post(
        "/matches", json=request,
        headers={**integration_headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()["match_id"]


def test_exception_queue_search_filters_and_pagination(client, reviewer_headers, integration_headers):
    first = submit_exception(client, integration_headers)
    second = submit_exception(client, integration_headers)

    matches = client.get("/exceptions?q=PO-10291", headers=reviewer_headers)
    first_page = client.get("/exceptions?limit=1&offset=0", headers=reviewer_headers)
    second_page = client.get("/exceptions?limit=1&offset=1", headers=reviewer_headers)

    assert matches.status_code == 200 and len(matches.json()) == 2
    assert len(first_page.json()) == 1 and len(second_page.json()) == 1
    assert first_page.json()[0]["match_id"] != second_page.json()[0]["match_id"]
    assert {first, second} == {row["match_id"] for row in matches.json()}


def test_bulk_approval_is_atomic_and_records_each_decision(client, reviewer_headers, integration_headers):
    first, second = submit_exception(client, integration_headers), submit_exception(client, integration_headers)
    response = client.post(
        "/exceptions/bulk-approve", headers=reviewer_headers,
        json={"match_ids": [first, second], "comment": "Batch reviewed."},
    )
    assert response.status_code == 200
    assert {item["approval_status"] for item in response.json()} == {"APPROVED"}
    assert all(
        client.get(f"/matches/{match_id}/audit", headers=reviewer_headers).json()[-1]["details"]["comment"]
        == "Batch reviewed."
        for match_id in (first, second)
    )


def test_bulk_approval_rolls_back_when_any_item_is_invalid(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)
    response = client.post(
        "/exceptions/bulk-approve", headers=reviewer_headers,
        json={"match_ids": [match_id, "missing-match"], "comment": "Batch."},
    )
    assert response.status_code == 404
    match = client.get(f"/matches/{match_id}", headers=reviewer_headers)
    assert match.json()["approval_status"] == "PENDING"


def test_overdue_exceptions_escalate_and_are_filterable(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)
    with SessionLocal() as session:
        record = session.get(MatchRecord, match_id)
        record.due_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()

    overdue = client.get("/exceptions?overdue_only=true", headers=reviewer_headers)
    result = client.post("/exceptions/escalate-overdue", headers=reviewer_headers)
    escalated = client.get("/exceptions?escalated_only=true", headers=reviewer_headers)

    assert [item["match_id"] for item in overdue.json()] == [match_id]
    assert result.json() == {"escalated_count": 1, "match_ids": [match_id]}
    assert escalated.json()[0]["escalated"] is True
    assert escalated.json()[0]["required_role"] == "admin"


def test_submitter_cannot_approve_own_exception(client, reviewer_headers, monkeypatch):
    monkeypatch.setenv("INTEGRATION_TOKENS", json.dumps({
        "finance-manager": "test-integration-secret-token-for-unit-tests-0001",
    }))
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())
    created = client.post(
        "/matches", json=request,
        headers={"Authorization": "Bearer test-integration-secret-token-for-unit-tests-0001",
                 "Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 201
    decision = client.post(
        f"/exceptions/{created.json()['match_id']}/approve",
        headers=reviewer_headers, json={"comment": "Self approval attempt."},
    )
    assert decision.status_code == 403
    assert "submitter" in decision.json()["detail"]


def test_rule_versions_require_distinct_admin_creator_and_activator(
    client, admin_headers, second_admin_headers, integration_headers
):
    content = (ROOT / "config" / "rules.yaml").read_text() + "\n# reviewed policy revision\n"
    created = client.post("/rules/versions", headers=admin_headers, json={"content": content})
    assert created.status_code == 201
    rule = created.json()
    assert rule["version"] == 1
    assert rule["status"] == "DRAFT"

    self_activation = client.post(
        f"/rules/versions/{rule['id']}/activate", headers=admin_headers,
    )
    assert self_activation.status_code == 403
    activated = client.post(
        f"/rules/versions/{rule['id']}/activate", headers=second_admin_headers,
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "ACTIVE"

    listing = client.get("/rules/versions", headers=admin_headers)
    assert listing.status_code == 200
    assert listing.json()[0]["activated_by"] == "controller"
    history = client.get(f"/rules/versions/{rule['id']}/audit", headers=admin_headers)
    assert [event["event_type"] for event in history.json()] == [
        "RULE_VERSION_CREATED", "RULE_VERSION_ACTIVATED",
    ]

    match_id = submit_exception(client, integration_headers)
    match_history = client.get(f"/matches/{match_id}/audit", headers=admin_headers).json()
    assert match_history[0]["details"]["rule_version_id"] == rule["id"]
    assert match_history[0]["details"]["rule_digest"] == rule["digest"]

    next_content = content.replace("# reviewed policy revision", "# next reviewed policy revision")
    next_draft = client.post("/rules/versions", headers=admin_headers, json={"content": next_content})
    next_active = client.post(
        f"/rules/versions/{next_draft.json()['id']}/activate", headers=second_admin_headers,
    )
    assert next_active.status_code == 200
    retired_history = client.get(
        f"/rules/versions/{rule['id']}/audit", headers=admin_headers,
    ).json()
    assert retired_history[-1]["event_type"] == "RULE_VERSION_RETIRED"


def test_invalid_rule_draft_is_rejected(client, admin_headers):
    response = client.post(
        "/rules/versions", headers=admin_headers,
        json={"content": "rules: [not, a, mapping]"},
    )
    assert response.status_code == 422


def test_capture_creator_cannot_verify_own_document(
    client, integration_headers, reviewer_headers, monkeypatch
):
    monkeypatch.setenv("INTEGRATION_TOKENS", json.dumps({
        "finance-manager": "test-integration-secret-token-for-unit-tests-0001",
    }))
    captured = client.post(
        "/documents/extract", headers={
            **integration_headers,
            "X-Filename": "invoice.txt",
            "Content-Type": "text/plain",
        }, content=b"Invoice INV-SOD\nVendor: ABC Supplies\nA100 10 10.00",
    )
    assert captured.status_code == 201
    document_id = captured.json()["document_id"]
    invoice = {"invoice": {"number": "INV-SOD", "vendor": "ABC Supplies",
                           "items": [{"sku": "A100", "qty": 10, "price": 10}]}}
    self_verify = client.post(
        f"/documents/{document_id}/verify", headers=reviewer_headers, json=invoice,
    )
    assert self_verify.status_code == 403
    assert "preparer" in self_verify.json()["detail"]
