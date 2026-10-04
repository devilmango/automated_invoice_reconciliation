from __future__ import annotations

import json
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def submit_exception(client, integration_headers, idempotency_key=None):
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())
    headers = {
        **integration_headers,
        "Idempotency-Key": idempotency_key or str(uuid4()),
    }
    response = client.post("/matches", json=request, headers=headers)
    assert response.status_code == 201
    return response.json()["match_id"]


def test_exception_queue_requires_reviewer_authentication(client, integration_headers):
    submit_exception(client, integration_headers)

    response = client.get("/exceptions")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_reviewer_can_approve_exception_and_audit_identity(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)

    unauthenticated = client.post(
        f"/exceptions/{match_id}/approve",
        json={"comment": "Attempt without a token."},
    )
    response = client.post(
        f"/exceptions/{match_id}/approve",
        headers=reviewer_headers,
        json={"comment": "Supplier confirmed the short shipment."},
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json()["approval_status"] == "APPROVED"
    assert client.get("/exceptions", headers=reviewer_headers).json() == []
    closed = client.get("/exceptions?status=ALL", headers=reviewer_headers).json()
    assert closed[0]["approval_status"] == "APPROVED"

    audit = client.get(f"/matches/{match_id}/audit", headers=reviewer_headers)
    assert audit.status_code == 200
    assert [event["event_type"] for event in audit.json()] == [
        "MATCH_CREATED",
        "EXCEPTION_APPROVED",
    ]
    assert audit.json()[1]["actor"] == "finance-manager"
    assert audit.json()[1]["details"]["comment"] == "Supplier confirmed the short shipment."


def test_exception_can_only_be_decided_once(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)
    body = {"comment": "Reviewed."}

    first = client.post(f"/exceptions/{match_id}/approve", headers=reviewer_headers, json=body)
    second = client.post(f"/exceptions/{match_id}/reject", headers=reviewer_headers, json=body)

    assert first.status_code == 200
    assert second.status_code == 409


def test_reviewer_cannot_spoof_audit_identity_in_decision_body(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)

    response = client.post(
        f"/exceptions/{match_id}/approve",
        headers=reviewer_headers,
        json={"actor": "someone-else", "comment": "Trying to spoof identity."},
    )

    assert response.status_code == 422


def test_reviewer_can_reject_exception(client, reviewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)

    response = client.post(
        f"/exceptions/{match_id}/reject",
        headers=reviewer_headers,
        json={"comment": "Request a corrected invoice."},
    )

    assert response.status_code == 200
    assert response.json()["approval_status"] == "REJECTED"
    assert client.get(f"/matches/{match_id}", headers=reviewer_headers).json()["approval_status"] == "REJECTED"


def test_unknown_reviewer_token_is_rejected(client):
    response = client.get(
        "/exceptions",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401


def test_review_route_fails_closed_when_no_reviewers_are_configured(client, monkeypatch):
    monkeypatch.delenv("REVIEWER_TOKENS")

    response = client.get("/exceptions")

    assert response.status_code == 503


def test_review_route_fails_closed_for_short_configured_tokens(client, monkeypatch):
    monkeypatch.setenv(
        "REVIEWER_TOKENS",
        '{"finance-reviewer":{"token":"short","roles":["reviewer"]}}',
    )

    response = client.get(
        "/exceptions",
        headers={"Authorization": "Bearer short"},
    )

    assert response.status_code == 503


def test_viewer_can_read_but_cannot_approve(client, viewer_headers, integration_headers):
    match_id = submit_exception(client, integration_headers)

    queue_response = client.get("/exceptions", headers=viewer_headers)
    decision_response = client.post(
        f"/exceptions/{match_id}/approve",
        headers=viewer_headers,
        json={"comment": "Viewer cannot approve."},
    )

    assert queue_response.status_code == 200
    assert decision_response.status_code == 403


def test_match_submission_requires_integration_credentials(client):
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())

    response = client.post(
        "/matches",
        json=request,
        headers={"Idempotency-Key": str(uuid4())},
    )

    assert response.status_code == 401
