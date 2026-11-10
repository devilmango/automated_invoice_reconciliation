from __future__ import annotations

import json
import os
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


def test_near_duplicate_invoice_is_flagged_across_supplier_aliases(
    client, integration_headers, monkeypatch, tmp_path
):
    profiles = tmp_path / "vendor-profiles.yaml"
    profiles.write_text("""vendors:
  - name: ABC Supplies
    aliases: [ABC Supply Ltd.]
""", encoding="utf-8")
    monkeypatch.setenv("INVOICE_MATCH_VENDOR_PROFILES", str(profiles))
    first = sample_request()
    first["invoice"]["invoice_date"] = "2026-10-07"
    submitted = client.post("/matches", json=first, headers=match_headers(integration_headers))

    possible_duplicate = sample_request()
    possible_duplicate["po"]["number"] = "PO-OTHER"
    possible_duplicate["invoice"]["number"] = "INV-20440"
    possible_duplicate["invoice"]["vendor"] = "ABC Supply Ltd."
    possible_duplicate["invoice"]["invoice_date"] = "2026-10-08"
    response = client.post(
        "/matches", json=possible_duplicate, headers=match_headers(integration_headers)
    )

    assert submitted.status_code == 201
    assert response.status_code == 201
    assert response.json()["reason"] == "DUPLICATE_INVOICE_RISK"
    candidate = response.json()["duplicate_candidates"][0]
    assert candidate["match_id"] == submitted.json()["match_id"]
    assert "INVOICE_NUMBER_SIMILARITY" in " ".join(candidate["evidence"])


def test_separately_numbered_partial_receipts_and_invoices_accumulate(client, integration_headers):
    first = sample_request()
    first["receipt"] = {"number": "GR-PART-1", "items": [{"sku": "A100", "qty": 40}]}
    first["invoice"]["items"][0]["qty"] = 40
    first["invoice"]["number"] = "INV-PART-1"
    first_result = client.post("/matches", json=first, headers=match_headers(integration_headers))
    assert first_result.status_code == 201

    second = sample_request()
    second["receipt"] = {"number": "GR-PART-2", "items": [{"sku": "A100", "qty": 60}]}
    second["invoice"]["items"][0]["qty"] = 60
    second["invoice"]["number"] = "INV-PART-2"
    second_result = client.post("/matches", json=second, headers=match_headers(integration_headers))

    assert second_result.status_code == 201
    result = second_result.json()
    assert result["status"] == "MATCHED"
    assert result["po_line_balances"] == [{
        "sku": "A100", "ordered_qty": "100", "received_qty": "100",
        "cumulatively_invoiced_qty": "100", "remaining_to_receive_qty": "0",
        "remaining_invoiceable_qty": "0",
    }]

    overbill = sample_request()
    overbill["receipt"] = {"number": "GR-PART-2", "items": [{"sku": "A100", "qty": 60}]}
    overbill["invoice"]["items"][0]["qty"] = 5
    overbill["invoice"]["number"] = "INV-OVER-BILL"
    excessive = client.post("/matches", json=overbill, headers=match_headers(integration_headers))
    assert excessive.status_code == 201
    assert excessive.json()["reason"] == "RECEIPT_QUANTITY_MISMATCH"
    assert excessive.json()["po_line_balances"][0]["cumulatively_invoiced_qty"] == "105"


def test_duplicate_check_endpoint_returns_candidates_without_creating_a_match(client, integration_headers):
    first = sample_request()
    first["invoice"]["invoice_date"] = "2026-10-07"
    submitted = client.post("/matches", json=first, headers=match_headers(integration_headers))
    suspicious = sample_request()
    suspicious["po"]["number"] = "PO-OTHER"
    suspicious["invoice"]["number"] = "INV-20440"
    suspicious["invoice"]["invoice_date"] = "2026-10-08"

    assessment = client.post("/matches/duplicate-check", json=suspicious, headers=integration_headers)

    assert submitted.status_code == 201
    assert assessment.status_code == 200
    assert assessment.json()["status"] == "REVIEW"
    assert assessment.json()["recommended_action"] == "REVIEW_CANDIDATES"
    assert assessment.json()["candidates"][0]["match_id"] == submitted.json()["match_id"]


def test_duplicate_check_uses_line_similarity_when_invoice_number_and_date_differ(client, integration_headers):
    first = sample_request()
    first["invoice"]["number"] = "INVOICE2026ABC0000441"
    submitted = client.post("/matches", json=first, headers=match_headers(integration_headers))
    candidate = sample_request()
    candidate["po"]["number"] = "PO-DIFFERENT"
    candidate["invoice"]["number"] = "INVOICE2026ABC1000442"

    assessment = client.post("/matches/duplicate-check", json=candidate, headers=integration_headers)

    assert submitted.status_code == 201
    assert assessment.status_code == 200
    assert assessment.json()["status"] == "REVIEW"
    assert any(item.startswith("INVOICE_LINE_SIMILARITY")
               for item in assessment.json()["candidates"][0]["evidence"])


def test_approval_policy_simulator_previews_roles_delegation_and_self_approval(client, admin_headers):
    documents = sample_request()
    content = """rules:
  currency:
    base_currency: USD
    rates_to_base: {USD: 1}
  approval:
    policies:
      - name: two-step-high-value
        min_invoice_total: 500
        required_role: approver
        approvals_required: 2
        assigned_to: primary
        delegates: [backup]
        sla_hours: 12
        escalation_role: admin
"""
    response = client.post("/admin/approval-policies/simulate", headers=admin_headers, json={
        "content": content,
        "samples": [{
            "name": "high value exception", "documents": documents, "submitted_by": "primary",
            "reviewers": {"primary": ["approver"], "backup": ["approver"]},
        }],
    })

    assert response.status_code == 200
    preview = response.json()
    assert preview["saved"] is False
    route = preview["results"][0]
    assert route["approval_policy"] == "two-step-high-value"
    assert route["assigned_to"] == "primary"
    assert route["delegates"] == ["backup"]
    assert route["eligible_approvers"] == ["backup"]
    assert route["sla_hours"] == 12
    assert route["separation_of_duties"]["submitter_was_excluded"] is True
    assert "INSUFFICIENT_DISTINCT_ELIGIBLE_APPROVERS" in route["warnings"]


def test_configured_policy_delegate_can_approve_and_is_audited(client, integration_headers,
                                                               admin_headers, reviewer_headers,
                                                               monkeypatch, tmp_path):
    reviewer_config = json.loads(os.environ["REVIEWER_TOKENS"])
    reviewer_config["finance-delegate"] = {
        "token": "test-finance-delegate-token-with-32-chars-0001",
        "roles": ["reviewer", "approver"],
    }
    monkeypatch.setenv("REVIEWER_TOKENS", json.dumps(reviewer_config))
    rules_file = tmp_path / "delegated-rules.yaml"
    rules_file.write_text("""rules:
  currency:
    base_currency: USD
    rates_to_base: {USD: 1}
  approval:
    policies:
      - name: assigned-with-delegate
        required_role: approver
        approvals_required: 2
        assigned_to: finance-manager
        delegates: [finance-delegate]
""", encoding="utf-8")
    monkeypatch.setenv("INVOICE_MATCH_RULES", str(rules_file))
    created = client.post("/matches", json=sample_request(),
                          headers=match_headers(integration_headers, "delegate-approval-sample"))
    assert created.status_code == 201
    response = client.post(f"/exceptions/{created.json()['match_id']}/approve",
        headers={"Authorization": "Bearer test-finance-delegate-token-with-32-chars-0001"},
        json={"comment": "Approved under assigned reviewer delegation."})
    assert response.status_code == 200
    assert response.json()["approval_status"] == "PENDING"
    primary_vote = client.post(f"/exceptions/{created.json()['match_id']}/approve",
        headers=reviewer_headers, json={"comment": "The delegate already voted."})
    assert primary_vote.status_code == 409
    independent_vote = client.post(f"/exceptions/{created.json()['match_id']}/approve",
        headers=admin_headers, json={"comment": "Independent second reviewer."})
    assert independent_vote.status_code == 200
    assert independent_vote.json()["approval_status"] == "APPROVED"

    from invoice_match.database import AuditEvent, SessionLocal
    from sqlalchemy import select
    with SessionLocal() as session:
        event = session.scalar(select(AuditEvent).where(
            AuditEvent.match_id == created.json()["match_id"],
            AuditEvent.event_type == "EXCEPTION_APPROVAL_RECORDED"))
        assert event.actor == "finance-delegate"
        assert event.details["delegated_from"] == "finance-manager"


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
