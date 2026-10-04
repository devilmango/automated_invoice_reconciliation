from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def submit_exception(client):
    request = json.loads((ROOT / "examples" / "match-request.json").read_text())
    response = client.post("/matches", json=request, headers={"X-Actor": "ap-import"})
    assert response.status_code == 201
    return response.json()["match_id"]


def test_exception_queue_requires_reviewer_authentication(client):
    submit_exception(client)

    response = client.get("/exceptions")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_reviewer_can_approve_exception_and_audit_identity(client, reviewer_headers):
    match_id = submit_exception(client)

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
    assert audit.json()[1]["actor"] == "finance-reviewer"
    assert audit.json()[1]["details"]["comment"] == "Supplier confirmed the short shipment."


def test_exception_can_only_be_decided_once(client, reviewer_headers):
    match_id = submit_exception(client)
    body = {"comment": "Reviewed."}

    first = client.post(f"/exceptions/{match_id}/approve", headers=reviewer_headers, json=body)
    second = client.post(f"/exceptions/{match_id}/reject", headers=reviewer_headers, json=body)

    assert first.status_code == 200
    assert second.status_code == 409


def test_reviewer_cannot_spoof_audit_identity_in_decision_body(client, reviewer_headers):
    match_id = submit_exception(client)

    response = client.post(
        f"/exceptions/{match_id}/approve",
        headers=reviewer_headers,
        json={"actor": "someone-else", "comment": "Trying to spoof identity."},
    )

    assert response.status_code == 422


def test_reviewer_can_reject_exception(client, reviewer_headers):
    match_id = submit_exception(client)

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
    monkeypatch.setenv("REVIEWER_TOKENS", '{"finance-reviewer":"short"}')

    response = client.get(
        "/exceptions",
        headers={"Authorization": "Bearer short"},
    )

    assert response.status_code == 503
