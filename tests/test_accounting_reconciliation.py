from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet

from test_operations import submit
from invoice_match.database import APOutboxRecord, AuditEvent, SessionLocal
from invoice_match.delivery import deliver_pending


ROOT = Path(__file__).resolve().parents[1]


def matched_invoice(client, integration_headers, *, number="INV-PAY-01", key=None):
    data = json.loads((ROOT / "examples" / "match-request.json").read_text())
    data["receipt"]["items"][0]["qty"] = data["po"]["items"][0]["qty"]
    data["invoice"]["number"] = number
    data["po"]["number"] = f"PO-{number}"
    return submit(client, integration_headers, data, key or f"payment-{uuid4().hex[:10]}")


def acknowledge(client, integration_headers, match_id, external="accounting-id-1"):
    response = client.post(f"/integrations/ap/outbox/{match_id}/ack", headers=integration_headers,
                           json={"external_reference": external})
    assert response.status_code == 200


def test_accounting_connection_is_encrypted_and_secret_is_never_returned(
    client, admin_headers, monkeypatch
):
    monkeypatch.setenv("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    secret = "test-client-secret-not-returned"
    response = client.put("/admin/accounting-connections/quickbooks", headers=admin_headers,
                          json={"configuration": {"client_id": "client", "client_secret": secret,
                                  "realm_id": "123", "refresh_token": "refresh",
                                  "token_encryption_key": Fernet.generate_key().decode(),
                                  "vendor_map": {}, "item_map": {}}, "enabled": True})
    assert response.status_code == 200
    assert response.json()["provider"] == "quickbooks"
    assert secret not in response.text
    listing = client.get("/admin/accounting-connections", headers=admin_headers)
    assert listing.status_code == 200
    assert listing.json()[0]["account_id"] == "123"
    assert secret not in listing.text


def test_remittance_auto_matches_exact_invoice_and_is_idempotent(
    client, integration_headers, reviewer_headers
):
    match = matched_invoice(client, integration_headers)
    acknowledge(client, integration_headers, match["match_id"])
    amount = str(match["invoice_total"])
    body = {"external_id": "bank-payment-001", "vendor": match["vendor"],
            "currency": "USD", "amount": amount, "reference": match["invoice_number"]}
    created = client.post("/integrations/payments/remittances", headers=integration_headers, json=body)
    assert created.status_code == 201
    assert created.json()["status"] == "MATCHED"
    assert created.json()["allocations"][0]["match_id"] == match["match_id"]
    replay = client.post("/integrations/payments/remittances", headers=integration_headers, json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == created.json()["id"]
    queue = client.get("/reviewer/payments", headers=reviewer_headers)
    assert queue.status_code == 200
    assert len(queue.json()) == 1


def test_remittance_ambiguous_payment_requires_review_then_manual_allocation(
    client, integration_headers, reviewer_headers
):
    first = matched_invoice(client, integration_headers, number="INV-AMB-1")
    second = matched_invoice(client, integration_headers, number="INV-AMB-2")
    acknowledge(client, integration_headers, first["match_id"], "acct-1")
    acknowledge(client, integration_headers, second["match_id"], "acct-2")
    amount = str(first["invoice_total"])
    response = client.post("/integrations/payments/remittances", headers=integration_headers,
        json={"external_id": "ambiguous-bank-id", "vendor": first["vendor"], "currency": "USD",
              "amount": amount})
    assert response.status_code == 201
    assert response.json()["status"] == "NEEDS_REVIEW"
    allocation = client.post(f"/reviewer/payments/{response.json()['id']}/allocate",
        headers=reviewer_headers, json={"allocations": [{"match_id": first["match_id"], "amount": amount}]})
    assert allocation.status_code == 200
    assert allocation.json()["status"] == "MATCHED"
    events = client.get(f"/reviewer/payments/{response.json()['id']}/events", headers=reviewer_headers)
    assert events.status_code == 200
    assert events.json()[-1]["event_type"] == "PAYMENT_MANUALLY_ALLOCATED"


def test_accounting_status_sync_audits_provider_status(client, integration_headers, admin_headers, monkeypatch):
    match = matched_invoice(client, integration_headers, number="INV-STATUS-1")
    acknowledge(client, integration_headers, match["match_id"], "provider-bill-1")

    class StatusAdapter:
        name = "test-provider"

        def fetch_invoice_status(self, external_reference):
            assert external_reference == "provider-bill-1"
            return {"status": "PAID", "amount_due": "0"}

    monkeypatch.setattr("invoice_match.api.create_adapter_for_company", lambda _company: StatusAdapter())
    synced = client.post(f"/reviewer/ap-outbox/{match['match_id']}/sync-status", headers=admin_headers)
    assert synced.status_code == 200
    assert synced.json()["accounting_status"] == "PAID"
    audit = client.get(f"/matches/{match['match_id']}/audit", headers=admin_headers).json()
    assert any(row["event_type"] == "AP_ACCOUNTING_STATUS_UPDATED" for row in audit)


def test_admin_can_manage_provider_mapping_categories(client, admin_headers, monkeypatch):
    monkeypatch.setenv("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    config = {"client_id": "client", "client_secret": "secret", "realm_id": "123",
              "refresh_token": "refresh", "token_encryption_key": Fernet.generate_key().decode(),
              "vendor_map": {}, "item_map": {}, "tax_code_map": {}, "currency_map": {}}
    saved = client.put("/admin/accounting-connections/quickbooks", headers=admin_headers,
                       json={"configuration": config, "enabled": True})
    assert saved.status_code == 200
    updated = client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"ABC Supplies": "vendor-37"}})
    assert updated.status_code == 200
    mapping = client.get("/admin/accounting-connections/quickbooks/mappings", headers=admin_headers)
    assert mapping.status_code == 200
    assert mapping.json()["mappings"]["supplier"] == {"ABC Supplies": "vendor-37"}
    denied = client.put("/admin/accounting-connections/quickbooks/mappings/item",
        headers={"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0001"},
        json={"mappings": {"A100": "item-12"}})
    assert denied.status_code == 403
    deleted = client.delete("/admin/accounting-connections/quickbooks/mappings/supplier/ABC%20Supplies",
                            headers=admin_headers)
    assert deleted.status_code == 200


def test_mapping_preflight_blocks_retry_and_reports_missing_ids(
    client, integration_headers, reviewer_headers, admin_headers, monkeypatch
):
    match = matched_invoice(client, integration_headers, number="INV-MAP-01")

    class MappingAdapter:
        name = "quickbooks"
        vendor_map = {}
        item_map = {}
        tax_code_map = {}
        currency_map = {}

        def submit_invoice(self, payload, *, idempotency_key):
            return "bill-100"

    adapter = MappingAdapter()
    monkeypatch.setattr("invoice_match.api.create_adapter_for_company", lambda _company: adapter)
    queue = client.get("/reviewer/ap-outbox?state=ALL", headers=reviewer_headers)
    issues = queue.json()[0]["mapping_issues"]
    assert {issue["category"] for issue in issues} == {"supplier", "item"}
    check = client.get(f"/reviewer/ap-outbox/{match['match_id']}/mapping-check", headers=reviewer_headers)
    assert check.status_code == 200
    assert check.json()["ready"] is False
    blocked = client.post(f"/reviewer/ap-outbox/{match['match_id']}/retry", headers=admin_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ACCOUNTING_MAPPINGS_MISSING"
    audit = client.get(f"/matches/{match['match_id']}/audit", headers=reviewer_headers).json()
    assert any(event["event_type"] == "AP_EXPORT_BLOCKED_MISSING_MAPPING" for event in audit)

    adapter.vendor_map = {"abc supplies": "vendor-37"}
    adapter.item_map = {"a100": "item-12"}
    ready = client.get(f"/reviewer/ap-outbox/{match['match_id']}/mapping-check", headers=reviewer_headers)
    assert ready.json()["ready"] is True
    replayed = client.post(f"/reviewer/ap-outbox/{match['match_id']}/retry", headers=admin_headers)
    assert replayed.status_code == 200
    assert replayed.json()["state"] == "ACKNOWLEDGED"


def test_dead_letter_filter_attempt_history_and_admin_replay(
    client, integration_headers, reviewer_headers, admin_headers, monkeypatch
):
    match = matched_invoice(client, integration_headers, number="INV-DEAD-01")
    with SessionLocal() as session:
        item = session.get(APOutboxRecord, match["match_id"])
        item.state = "DEAD"
        item.attempt_count = 8
        item.last_error = "provider rejected export"
        session.add(AuditEvent(match_id=match["match_id"], event_type="AP_EXPORT_FAILED",
                               actor="adapter:quickbooks", details={"attempt": 8,
                               "state": "DEAD", "error": item.last_error}))
        session.commit()

    class ReplayAdapter:
        name = "test-ap"

        def submit_invoice(self, payload, *, idempotency_key):
            return "bill-after-replay"

    monkeypatch.setattr("invoice_match.api.create_adapter_for_company", lambda _company: ReplayAdapter())
    dead = client.get("/reviewer/ap-outbox?state=DEAD", headers=reviewer_headers)
    assert dead.status_code == 200
    assert len(dead.json()) == 1
    history = client.get(f"/reviewer/ap-outbox/{match['match_id']}/history", headers=reviewer_headers)
    assert history.status_code == 200
    assert history.json()[0]["event_type"] == "AP_EXPORT_FAILED"
    replayed = client.post(f"/reviewer/ap-outbox/{match['match_id']}/retry", headers=admin_headers)
    assert replayed.status_code == 200
    assert replayed.json()["state"] == "ACKNOWLEDGED"
    assert replayed.json()["attempt_count"] == 9


def test_delivery_worker_blocks_connector_export_when_required_mappings_are_missing(
    client, integration_headers
):
    match = matched_invoice(client, integration_headers, number="INV-WORKER-MAP")

    class QuickBooksAdapter:
        name = "quickbooks"
        vendor_map = {}
        item_map = {}
        tax_code_map = {}
        currency_map = {}

        def submit_invoice(self, *_args, **_kwargs):
            raise AssertionError("Preflight should prevent a provider call")

    with SessionLocal() as session:
        result = deliver_pending(session, QuickBooksAdapter())
        item = session.get(APOutboxRecord, match["match_id"])
        assert result["blocked"] == 1
        assert result["dead_lettered"] == 0
        assert item.state == "DEAD"
        assert item.attempt_count == 0
        assert "Missing accounting mappings" in item.last_error
