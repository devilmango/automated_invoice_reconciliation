from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta

from cryptography.fernet import Fernet

from invoice_match.accounting_connections import encrypt_configuration
from invoice_match.database import (
    AccountingConnectionEventRecord,
    AccountingConnectionRecord,
    AccountingMasterDataRecord,
    MatchRecord,
    NotificationOutboxRecord,
    SessionLocal,
)


def seed_admin_data(monkeypatch, *, second_vendor: bool = False):
    monkeypatch.setenv("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    config = {
        "client_id": "client", "client_secret": "secret", "refresh_token": "refresh",
        "token_encryption_key": Fernet.generate_key().decode(), "realm_id": "12345",
        "vendor_map": {"Retired Vendor": "inactive-vendor"}, "item_map": {}, "tax_code_map": {},
    }
    with SessionLocal() as session:
        session.add(AccountingConnectionRecord(id="controls-connection", provider="quickbooks",
            encrypted_config=encrypt_configuration(config), enabled=True, updated_by="admin"))
        session.add_all([
            AccountingMasterDataRecord(id="controls-vendor", provider="quickbooks", category="supplier",
                source_id="vendor-1", code="ABC", name="ABC Supplies", active=True, source_data={},
                synced_at=datetime.now(timezone.utc)),
            AccountingMasterDataRecord(id="inactive-vendor", provider="quickbooks", category="supplier",
                source_id="inactive-vendor", code="OLD", name="Retired Vendor", active=False,
                source_data={}, synced_at=datetime.now(timezone.utc)),
            AccountingMasterDataRecord(id="removed-vendor", provider="quickbooks", category="supplier",
                source_id="removed-vendor", code="REMOVE", name="Removed Vendor", active=True,
                source_data={}, synced_at=datetime.now(timezone.utc)),
            AccountingMasterDataRecord(id="controls-item", provider="quickbooks", category="item",
                source_id="item-1", code="SKU-1", name="Widget One", active=True, source_data={},
                synced_at=datetime.now(timezone.utc)),
            AccountingMasterDataRecord(id="controls-tax", provider="quickbooks", category="tax",
                source_id="tax-1", code="TAX-1", name="Standard Tax", active=True, source_data={},
                synced_at=datetime.now(timezone.utc)),
        ])
        if second_vendor:
            session.add(AccountingMasterDataRecord(id="controls-vendor-2", provider="quickbooks",
                category="supplier", source_id="vendor-2", code="ABC", name="ABC Supplies", active=True,
                source_data={}, synced_at=datetime.now(timezone.utc)))
        session.add(MatchRecord(id="controls-match", status="MATCHED", approval_status="PENDING",
            po_number="PO-CTRL", invoice_number="INV-CTRL", vendor="ABC Supplies", vendor_key="abc supplies",
            approval_policy="default", required_role="approver", escalation_role="admin",
            approvals_required=1, approvals_received=0, rule_digest="", rule_snapshot={},
            input_data={"invoice": {"vendor": "ABC Supplies", "items": [
                {"sku": "SKU-1", "tax_code": "TAX-1"},
                {"sku": "SKU-MISSING", "tax_code": "TAX-MISSING"},
            ]}}, result_data={}))
        session.commit()


def test_master_sync_history_records_run_duration_and_record_changes(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        name = "quickbooks"

        def fetch_master_data(self):
            return {"supplier": [
                {"source_id": "vendor-1", "name": "ABC Supply Co", "code": "ABC", "active": True},
                {"source_id": "vendor-2", "name": "New Vendor", "active": True},
            ]}

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    response = client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers)
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "SUCCEEDED"
    assert run["duration_ms"] >= 0
    assert run["changes"]["supplier"] == {"added": 1, "changed": 1, "deactivated": 1}
    history = client.get("/admin/accounting-connections/quickbooks/sync-history", headers=admin_headers)
    assert history.status_code == 200 and history.json()[0]["id"] == run["id"]


def test_mapping_coverage_reports_unmapped_labels_inactive_targets_and_ambiguity(
    client, admin_headers, monkeypatch,
):
    seed_admin_data(monkeypatch, second_vendor=True)
    response = client.get("/admin/accounting-connections/quickbooks/mapping-coverage", headers=admin_headers)
    assert response.status_code == 200
    report = response.json()
    assert {item["source_label"] for item in report["unmapped"]} >= {
        "ABC Supplies", "SKU-MISSING", "TAX-MISSING",
    }
    assert report["inactive_mappings"][0]["source_label"] == "Retired Vendor"
    ambiguous = next((item for item in report["ambiguous_suggestions"] if item["source_label"] == "ABC Supplies"), None)
    assert ambiguous is not None
    assert {candidate["source_id"] for candidate in ambiguous["candidates"]} == {"vendor-1", "vendor-2"}


def test_provider_connection_test_separates_authentication_failures(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        def test_connection(self):
            raise RuntimeError("OAuth token refresh failed: invalid_grant")

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    response = client.post("/admin/accounting-connections/quickbooks/test-connection", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["failure_type"] == "AUTHENTICATION"
    status = client.get("/admin/accounting-connections/sync-status", headers=admin_headers).json()[0]
    assert status["last_error"] is None
    assert status["connection_health"]["failure_type"] == "AUTHENTICATION"


def test_failed_master_sync_queues_provider_webhook_alert_with_admin_link(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        name = "quickbooks"

        def fetch_master_data(self):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    response = client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers)
    assert response.status_code == 502
    history = client.get("/admin/accounting-connections/quickbooks/sync-history", headers=admin_headers).json()
    assert history[0]["status"] == "FAILED" and history[0]["error"] == "provider unavailable"
    with SessionLocal() as session:
        alert = session.query(NotificationOutboxRecord).filter_by(event_type="MASTER_DATA_SYNC_FAILED").one()
        assert alert.match_id is None
        assert alert.payload["provider"] == "quickbooks"
        assert "/reviewer/operations" in alert.payload["admin_url"]
    listed = client.get("/notifications/outbox?state=ALL", headers=admin_headers)
    assert listed.status_code == 200 and listed.json()[0]["provider"] == "quickbooks"
    assert listed.json()[0]["match_id"] is None


def test_admin_can_inspect_and_rollback_mapping_change(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    saved = client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"ABC Supplies": "vendor-1"}})
    assert saved.status_code == 200
    history = client.get("/admin/accounting-connections/quickbooks/mapping-history", headers=admin_headers)
    assert history.status_code == 200
    change = next(item for item in history.json() if item["source_label"] == "ABC Supplies")
    rollback = client.post(
        f"/admin/accounting-connections/quickbooks/mapping-history/{change['id']}/rollback",
        headers=admin_headers,
    )
    assert rollback.status_code == 200
    mappings = client.get("/admin/accounting-connections/quickbooks/mappings", headers=admin_headers).json()
    assert "ABC Supplies" not in mappings["mappings"]["supplier"]
    history = client.get("/admin/accounting-connections/quickbooks/mapping-history", headers=admin_headers)
    assert any(item["change_type"] == "ROLLBACK" and item["reverts_change_id"] == change["id"]
               for item in history.json())


def test_mapping_rollback_refuses_to_overwrite_a_newer_mapping(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"ABC Supplies": "vendor-1"}})
    history = client.get("/admin/accounting-connections/quickbooks/mapping-history", headers=admin_headers).json()
    original = next(item for item in history if item["source_label"] == "ABC Supplies")
    client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"ABC Supplies": "vendor-2"}})
    response = client.post(
        f"/admin/accounting-connections/quickbooks/mapping-history/{original['id']}/rollback",
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_stale_provider_sync_queues_a_deduplicated_webhook_alert(monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import MasterDataSyncScheduleRecord
    from invoice_match.master_data_sync import queue_stale_master_data_alerts
    from invoice_match.notification_delivery import deliver_pending_notifications

    with SessionLocal() as session:
        session.add(MasterDataSyncScheduleRecord(id="stale-schedule", provider="quickbooks",
            enabled=True, interval_minutes=60, stale_after_minutes=30,
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=2)))
        session.commit()
        assert queue_stale_master_data_alerts(session) == 1
        assert queue_stale_master_data_alerts(session) == 0
        session.commit()
        alert = session.query(NotificationOutboxRecord).filter_by(event_type="MASTER_DATA_SYNC_STALE").one()
        assert alert.payload["provider"] == "quickbooks"
        assert "view=master-data-sync" in alert.payload["admin_url"]

        class Webhook:
            name = "webhook"

            def deliver(self, payload, *, idempotency_key):
                assert payload["event_type"] == "MASTER_DATA_SYNC_STALE"
                assert idempotency_key == alert.dedupe_key
                return "webhook-event-1"

        result = deliver_pending_notifications(session, Webhook())
        assert result["delivered"] == 1
        session.commit()
        assert session.query(AccountingConnectionEventRecord).filter_by(
            event_type="ADMIN_ALERT_DELIVERED", provider="quickbooks").count() == 1


def test_provider_connection_health_can_recover_after_authentication_failure(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        def test_connection(self):
            return {"account_id": "12345", "account_name": "Acme Books"}

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    response = client.post("/admin/accounting-connections/quickbooks/test-connection", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "HEALTHY"
    assert response.json()["account_name"] == "Acme Books"
