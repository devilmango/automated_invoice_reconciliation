from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta

from cryptography.fernet import Fernet

from invoice_match.accounting_connections import encrypt_configuration
from invoice_match.database import (
    AccountingConnectionEventRecord,
    AccountingConnectionRecord,
    AccountingMappingChangeRecord,
    AccountingMasterDataRecord,
    MasterDataSyncRunRecord,
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
    assert {item["action"] for item in run["details"]["supplier"]} == {"ADDED", "CHANGED", "DEACTIVATED"}
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
    with SessionLocal() as session:
        session.add(AccountingMasterDataRecord(id="rollback-vendor-2", provider="quickbooks",
            category="supplier", source_id="vendor-2", name="ABC Supply Alternate", active=True,
            source_data={}, synced_at=datetime.now(timezone.utc)))
        session.commit()
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


def test_sync_preview_shows_record_level_changes_without_applying_them(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        name = "quickbooks"

        def fetch_master_data(self):
            return {"supplier": [
                {"source_id": "vendor-1", "name": "ABC Supplies Updated", "code": "ABC", "active": True},
                {"source_id": "vendor-new", "name": "New Vendor", "active": True},
            ]}

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    response = client.post("/admin/accounting-connections/quickbooks/sync-preview", headers=admin_headers)
    assert response.status_code == 200
    preview = response.json()
    assert preview["applies_changes"] is False
    assert preview["changes"]["supplier"] == {"added": 1, "changed": 1, "deactivated": 1}
    assert {item["action"] for item in preview["details"]["supplier"]} == {
        "ADDED", "CHANGED", "DEACTIVATED",
    }
    with SessionLocal() as session:
        record = session.query(AccountingMasterDataRecord).filter_by(
            provider="quickbooks", source_id="vendor-1").one()
        assert record.name == "ABC Supplies"
        assert session.query(AccountingMasterDataRecord).filter_by(source_id="vendor-new").count() == 0


def test_risky_sync_requires_second_admin_and_applies_approved_snapshot(client, admin_headers,
                                                                          second_admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api
    from invoice_match.database import MasterDataSyncScheduleRecord

    class Adapter:
        name = "quickbooks"

        def fetch_master_data(self):
            return {"supplier": [{"source_id": "vendor-1", "name": "ABC Supplies", "active": True}]}

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    with SessionLocal() as session:
        session.add(MasterDataSyncScheduleRecord(id="guard-schedule", provider="quickbooks",
            enabled=False, interval_minutes=1440, stale_after_minutes=2880,
            max_deactivations=0, max_diff_records=100))
        session.commit()

    preview = client.post("/admin/accounting-connections/quickbooks/sync-preview", headers=admin_headers)
    assert preview.status_code == 200
    body = preview.json()
    assert body["approval_required"] is True
    assert body["risk"]["reasons"] == ["BULK_DEACTIVATION"]
    approval_id = body["approval_id"]
    assert client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers).status_code == 409
    same_admin = client.post(
        f"/admin/accounting-connections/quickbooks/sync-approvals/{approval_id}/approve",
        headers=admin_headers,
    )
    assert same_admin.status_code == 403
    approved = client.post(
        f"/admin/accounting-connections/quickbooks/sync-approvals/{approval_id}/approve",
        headers=second_admin_headers,
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    applied = client.post(f"/admin/accounting-connections/quickbooks/sync-masters?approval_id={approval_id}",
                          headers=admin_headers)
    assert applied.status_code == 200
    with SessionLocal() as session:
        approval = session.get(api.MasterDataSyncApprovalRecord, approval_id)
        assert approval.status == "APPLIED" and approval.run_id == applied.json()["run"]["id"]


def test_risky_sync_rejects_provider_snapshot_changed_after_approval(client, admin_headers,
                                                                      second_admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api
    from invoice_match.database import MasterDataSyncScheduleRecord
    from invoice_match.database import MasterDataSyncApprovalRecord

    class Adapter:
        name = "quickbooks"
        changed = False

        def fetch_master_data(self):
            suffix = " changed" if self.changed else ""
            return {"supplier": [{"source_id": "vendor-1", "name": "ABC Supplies" + suffix, "active": True}]}

    adapter = Adapter()
    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: adapter)
    with SessionLocal() as session:
        session.add(MasterDataSyncScheduleRecord(id="stale-guard-schedule", provider="quickbooks",
            enabled=False, interval_minutes=1440, stale_after_minutes=2880,
            max_deactivations=0, max_diff_records=100))
        session.commit()
    preview = client.post("/admin/accounting-connections/quickbooks/sync-preview", headers=admin_headers).json()
    approval_id = preview["approval_id"]
    assert client.post(
        f"/admin/accounting-connections/quickbooks/sync-approvals/{approval_id}/approve",
        headers=second_admin_headers,
    ).status_code == 200
    adapter.changed = True
    response = client.post(f"/admin/accounting-connections/quickbooks/sync-masters?approval_id={approval_id}",
                           headers=admin_headers)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "SYNC_PREVIEW_STALE"
    with SessionLocal() as session:
        assert session.get(MasterDataSyncApprovalRecord, approval_id).status == "STALE"


def test_mapping_validation_rejects_unknown_inactive_and_duplicate_targets(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    invalid = client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"Missing": "does-not-exist"}})
    assert invalid.status_code == 422
    inactive = client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"Retired": "inactive-vendor"}})
    assert inactive.status_code == 422
    duplicate = client.put("/admin/accounting-connections/quickbooks/mappings/supplier",
        headers=admin_headers, json={"mappings": {"ABC": "vendor-1", "ABC alias": "vendor-1"}})
    assert duplicate.status_code == 422
    report = client.get("/admin/accounting-connections/quickbooks/mapping-coverage",
        headers=admin_headers)
    assert report.status_code == 200
    assert any(item["type"] == "UNKNOWN_OR_INACTIVE_TARGET"
        for item in report.json()["mapping_conflicts"])


def test_scheduled_provider_connection_check_persists_auth_failure_and_reconnect_state(monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import AccountingConnectionHealthRecord, MasterDataSyncScheduleRecord
    from invoice_match.master_data_sync import check_due_provider_connections

    with SessionLocal() as session:
        schedule = MasterDataSyncScheduleRecord(id="connection-check-due", provider="quickbooks",
            enabled=False, interval_minutes=60, stale_after_minutes=120,
            connection_check_enabled=True, connection_check_interval_minutes=30,
            next_connection_check_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        session.add(schedule)
        session.commit()

        class Adapter:
            def test_connection(self):
                raise RuntimeError("OAuth refresh failed: invalid_grant")

        result = check_due_provider_connections(session, company_id="default",
            adapter_factory=lambda provider, company: Adapter(), actor="worker")
        assert result == {"checked": 0, "failed": 1}
        session.refresh(schedule)
        health = session.query(AccountingConnectionHealthRecord).filter_by(provider="quickbooks").one()
        assert health.failure_type == "AUTHENTICATION"
        assert schedule.last_connection_check_at is not None
        assert schedule.next_connection_check_at > schedule.last_connection_check_at


def test_worker_schedules_checks_for_enabled_connections_without_admin_setup(monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import AccountingConnectionHealthRecord, MasterDataSyncScheduleRecord
    from invoice_match.master_data_sync import check_due_provider_connections

    class Adapter:
        def test_connection(self):
            return {"account_id": "qbo-123", "account_name": "ABC Books"}

    with SessionLocal() as session:
        result = check_due_provider_connections(session, company_id="default",
            adapter_factory=lambda provider, company: Adapter(), actor="worker")
        assert result == {"checked": 1, "failed": 0}
        schedule = session.query(MasterDataSyncScheduleRecord).filter_by(provider="quickbooks").one()
        health = session.query(AccountingConnectionHealthRecord).filter_by(provider="quickbooks").one()
        assert schedule.last_connection_check_at is not None
        assert health.account_id == "qbo-123"


def test_successful_sync_queues_recovery_notice_after_prior_failure(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match import api

    class Adapter:
        name = "quickbooks"
        fail = True

        def fetch_master_data(self):
            if self.fail:
                raise RuntimeError("provider temporarily unavailable")
            return {"supplier": [{"source_id": "vendor-1", "name": "ABC Supplies", "active": True}]}

    adapter = Adapter()
    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: adapter)
    failed = client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers)
    assert failed.status_code == 502
    adapter.fail = False
    recovered = client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers)
    assert recovered.status_code == 200
    alerts = client.get("/admin/alerts?state=ALL", headers=admin_headers)
    assert alerts.status_code == 200
    recovery = next(item for item in alerts.json() if item["event_type"] == "MASTER_DATA_SYNC_RECOVERED")
    assert recovery["payload"]["recovery_run_id"] == recovered.json()["run"]["id"]
    assert recovery["payload"]["failed_run_id"]
    assert recovery["resolves_alert_id"]
    resolved = next(item for item in alerts.json() if item["id"] == recovery["resolves_alert_id"])
    assert resolved["management_state"] == "RESOLVED"
    assert resolved["resolved_by_alert_id"] == recovery["id"]


def test_escalation_waits_for_snooze_expiry_and_is_idempotent(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import MasterDataSyncScheduleRecord, NotificationOutboxRecord
    from invoice_match.master_data_sync import escalate_unacknowledged_master_data_alerts

    with SessionLocal() as session:
        session.add(MasterDataSyncScheduleRecord(id="escalation-schedule", provider="quickbooks",
            enabled=False, interval_minutes=1440, stale_after_minutes=2880,
            alert_escalation_enabled=True, alert_escalation_minutes=5))
        session.add(NotificationOutboxRecord(id="old-alert", provider="quickbooks", match_id=None,
            dedupe_key="old-alert-once", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
            payload={"message": "Provider sync failed"}, state="DELIVERED", attempt_count=1,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            snoozed_until=datetime.now(timezone.utc) + timedelta(hours=1), snoozed_by="cfo"))
        session.commit()
        assert escalate_unacknowledged_master_data_alerts(session) == 0
        alert = session.get(NotificationOutboxRecord, "old-alert")
        alert.snoozed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        assert escalate_unacknowledged_master_data_alerts(session) == 1
        session.commit()
        assert escalate_unacknowledged_master_data_alerts(session) == 0
    listing = client.get("/admin/alerts?state=ALL", headers=admin_headers).json()
    original = next(item for item in listing if item["id"] == "old-alert")
    escalation = next(item for item in listing if item["event_type"] == "MASTER_DATA_SYNC_ESCALATED")
    assert original["escalated_at"]
    assert escalation["payload"]["source_alert_id"] == "old-alert"


def test_admin_can_acknowledge_and_snooze_provider_alerts(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import NotificationOutboxRecord

    with SessionLocal() as session:
        session.add(NotificationOutboxRecord(id="managed-alert", provider="quickbooks", match_id=None,
            dedupe_key="managed-alert-once", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
            payload={"message": "Sync failed"}, state="DELIVERED", attempt_count=2))
        session.commit()
    snoozed = client.post("/admin/alerts/managed-alert/snooze", headers=admin_headers,
        json={"snoozed_until": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()})
    assert snoozed.status_code == 200
    assert client.get("/admin/alerts?state=SNOOZED", headers=admin_headers).json()[0]["id"] == "managed-alert"
    acknowledged = client.post("/admin/alerts/managed-alert/acknowledge", headers=admin_headers)
    assert acknowledged.status_code == 200
    assert client.get("/admin/alerts?state=ACKNOWLEDGED", headers=admin_headers).json()[0]["management_state"] == "ACKNOWLEDGED"
    assert client.post("/admin/alerts/managed-alert/acknowledge",
        headers={"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0001"}).status_code == 403


def test_acknowledging_escalation_marks_original_alert(client, admin_headers):
    with SessionLocal() as session:
        session.add_all([
            NotificationOutboxRecord(id="source-failure", provider="quickbooks", match_id=None,
                dedupe_key="source-failure", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
                payload={"message": "sync failed"}, state="DELIVERED", attempt_count=1),
            NotificationOutboxRecord(id="escalation-notice", provider="quickbooks", match_id=None,
                dedupe_key="escalation-notice", event_type="MASTER_DATA_SYNC_ESCALATED", channel="webhook",
                payload={"source_alert_id": "source-failure"}, state="DELIVERED", attempt_count=1),
        ])
        session.commit()
    response = client.post("/admin/alerts/escalation-notice/acknowledge", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["source_alert_id"] == "source-failure"
    source = next(item for item in client.get("/admin/alerts?state=ALL", headers=admin_headers).json()
                  if item["id"] == "source-failure")
    assert source["escalation_acknowledged_by"] == "cfo"
    assert source["escalation_acknowledged_at"]


def test_audit_export_retention_removes_only_file_and_records_cleanup(client, admin_headers):
    import hashlib
    from invoice_match.database import AuditExportRecord

    generated = datetime.now(timezone.utc) - timedelta(days=3)
    content = b"csv evidence bytes"
    checksum = hashlib.sha256(content).hexdigest()
    manifest = {"export_id": "expired-export", "generated_by": "cfo",
        "date_range": {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        "filters": {"provider": None, "event_type": None, "actor": None},
        "checksum_sha256": checksum}
    with SessionLocal() as session:
        session.add(AuditExportRecord(id="expired-export", generated_by="cfo", start_date="2026-01-01",
            end_date="2026-01-02", checksum_sha256=checksum, row_count=0, manifest=manifest,
            file_content=content, created_at=generated))
        session.commit()
    assert client.put("/admin/accounting-connections/audit-export-retention", headers=admin_headers,
                      json={"retention_days": 1}).status_code == 200
    cleanup = client.post("/admin/accounting-connections/audit-exports/cleanup", headers=admin_headers)
    assert cleanup.status_code == 200
    assert cleanup.json()["cleaned_count"] == 1
    record = client.get("/admin/accounting-connections/audit-exports", headers=admin_headers).json()[0]
    assert record["artifact_available"] is False
    assert record["checksum_sha256"] == checksum
    assert client.get("/admin/accounting-connections/audit-exports/expired-export", headers=admin_headers).status_code == 410
    retained_manifest = client.get("/admin/accounting-connections/audit-exports/expired-export/manifest",
                                   headers=admin_headers)
    assert retained_manifest.status_code == 200
    history = client.get("/admin/accounting-connections/audit-exports/cleanup-history", headers=admin_headers).json()
    assert history[0]["export_id"] == "expired-export"
    assert history[0]["bytes_removed"] == len(content)


def test_alert_routes_select_provider_and_severity_specific_target(client, admin_headers):
    from invoice_match.database import AdminAlertRouteRecord
    from invoice_match.notification_delivery import WebhookNotificationAdapter, deliver_pending_notifications

    with SessionLocal() as session:
        session.add_all([
            AdminAlertRouteRecord(id="global-warning", provider_key="*", severity="WARNING",
                endpoint_url="https://hooks.example.com/warnings", enabled=True,
                created_by="cfo", updated_by="cfo"),
            AdminAlertRouteRecord(id="qb-critical", provider_key="quickbooks", severity="CRITICAL",
                endpoint_url="https://hooks.example.com/qb-critical", enabled=True,
                created_by="cfo", updated_by="cfo"),
            NotificationOutboxRecord(id="routed-failure", provider="quickbooks", match_id=None,
                dedupe_key="routed-failure", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
                payload={"message": "failed"}, state="PENDING", attempt_count=0),
        ])
        session.commit()

        class CaptureAdapter(WebhookNotificationAdapter):
            def __init__(self):
                super().__init__("https://hooks.example.com/default")
                self.target = None

            def deliver(self, payload, *, idempotency_key, endpoint=None):
                self.target = endpoint or self.endpoint
                return "captured"

        adapter = CaptureAdapter()
        summary = deliver_pending_notifications(session, adapter, limit=1)
        assert adapter.target == "https://hooks.example.com/qb-critical"
        assert summary["items"][0]["route_id"] == "qb-critical"

    created = client.post("/admin/alert-routes", headers=admin_headers, json={
        "provider": "xero", "severity": "CRITICAL", "endpoint_url": "https://hooks.example.com/xero",
        "enabled": True})
    assert created.status_code == 200
    assert client.post("/admin/alert-routes", headers=admin_headers, json={
        "provider": "xero", "severity": "CRITICAL", "endpoint_url": "https://hooks.example.com/other",
        "enabled": True}).status_code == 409


def test_routed_only_delivery_leaves_non_alert_notifications_pending():
    from invoice_match.notification_delivery import WebhookNotificationAdapter, deliver_pending_notifications

    with SessionLocal() as session:
        session.add(NotificationOutboxRecord(id="unrouted-reviewer-notice", provider=None, match_id=None,
            dedupe_key="unrouted-reviewer-notice", event_type="APPROVAL_DUE_SOON", channel="webhook",
            payload={}, state="PENDING", attempt_count=0))
        session.commit()
        adapter = WebhookNotificationAdapter(None)
        summary = deliver_pending_notifications(session, adapter, routed_only=True)
        assert summary["items"] == []
        assert session.get(NotificationOutboxRecord, "unrouted-reviewer-notice").state == "PENDING"


def test_admin_audit_export_includes_sync_mapping_connection_and_alert_records(
    client, admin_headers, monkeypatch,
):
    seed_admin_data(monkeypatch)
    from invoice_match.database import NotificationOutboxRecord

    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(MasterDataSyncRunRecord(id="export-run", provider="quickbooks",
            trigger_type="MANUAL", actor="cfo", status="SUCCEEDED", counts={"supplier": 1},
            changes={"supplier": {"added": 1}}, details={"supplier": []},
            started_at=now, finished_at=now, duration_ms=4))
        session.add(AccountingMappingChangeRecord(id="export-map", provider="quickbooks",
            category="supplier", source_label="ABC Supplies", previous_source_id=None,
            previous_source_label=None, new_source_id="vendor-1", new_source_label="ABC Supplies",
            change_type="ACCEPTED", actor="cfo"))
        session.add(AccountingConnectionEventRecord(provider="quickbooks",
            event_type="ACCOUNTING_CONNECTION_HEALTH_CHECKED", actor="worker",
            details={"status": "HEALTHY"}, created_at=now))
        session.add(NotificationOutboxRecord(id="export-alert", provider="quickbooks", match_id=None,
            dedupe_key="export-alert", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
            payload={"message": "Failure"}, state="DELIVERED", attempt_count=1, created_at=now))
        session.commit()
    response = client.get(
        f"/admin/accounting-connections/audit-export?start_date={now.date()}&end_date={now.date()}",
        headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="invoice-match-audit-' in response.headers["content-disposition"]
    assert "SYNC_RUN" in response.text
    assert "MAPPING_REVISION" in response.text
    assert "CONNECTION_CHECK" in response.text
    assert "ALERT" in response.text
    denied = client.get(f"/admin/accounting-connections/audit-export?start_date={now.date()}&end_date={now.date()}",
        headers={"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0001"})
    assert denied.status_code == 403


def test_audit_export_filters_manifest_checksum_and_saved_download(client, admin_headers, monkeypatch):
    import csv
    import hashlib
    import io
    import json

    seed_admin_data(monkeypatch)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(MasterDataSyncRunRecord(id="filtered-export-run", provider="quickbooks",
            trigger_type="MANUAL", actor="cfo", status="SUCCEEDED", counts={}, changes={},
            details={}, started_at=now, finished_at=now))
        session.add(MasterDataSyncRunRecord(id="other-export-run", provider="xero",
            trigger_type="MANUAL", actor="controller", status="FAILED", counts={}, changes={},
            details={}, error="timeout", started_at=now, finished_at=now))
        session.commit()

    response = client.get(
        f"/admin/accounting-connections/audit-export?start_date={now.date()}&end_date={now.date()}"
        "&provider=quickbooks&event_type=MANUAL&actor=cfo", headers=admin_headers)
    assert response.status_code == 200
    export_id = response.headers["x-audit-export-id"]
    checksum = response.headers["x-audit-export-sha256"]
    records = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    assert len(records) == 2
    assert records[0]["event_id"] == "filtered-export-run"
    assert records[-1]["record_type"] == "EXPORT_MANIFEST"
    manifest = json.loads(records[-1]["details"])
    assert manifest["generated_by"] == "cfo"
    assert manifest["date_range"] == {"start_date": now.date().isoformat(), "end_date": now.date().isoformat()}
    assert manifest["filters"] == {"provider": "quickbooks", "event_type": "manual", "actor": "cfo"}
    assert hashlib.sha256(response.content[:manifest["data_bytes"]]).hexdigest() == checksum
    assert manifest["checksum_sha256"] == checksum and manifest["row_count"] == 1

    history = client.get("/admin/accounting-connections/audit-exports", headers=admin_headers).json()
    saved = next(item for item in history if item["id"] == export_id)
    assert saved["generated_by"] == "cfo" and saved["row_count"] == 1
    assert saved["checksum_sha256"] == checksum
    assert client.get(saved["manifest_url"], headers=admin_headers).json() == manifest
    downloaded = client.get(saved["download_url"], headers=admin_headers)
    assert downloaded.content == response.content
    assert downloaded.headers["x-audit-export-sha256"] == checksum
    verified = client.post(f"/admin/accounting-connections/audit-exports/{export_id}/verify",
                           headers=admin_headers)
    assert verified.status_code == 200
    assert verified.json()["valid"] is True
    assert verified.json()["checks"]["embedded_manifest_matches_record"] is True
    from invoice_match.database import AuditExportRecord
    with SessionLocal() as session:
        saved_export = session.get(AuditExportRecord, export_id)
        saved_export.file_content = saved_export.file_content.replace(b"filtered-export-run", b"changed-export-run")
        session.commit()
    tampered = client.post(f"/admin/accounting-connections/audit-exports/{export_id}/verify",
                           headers=admin_headers)
    assert tampered.status_code == 200
    assert tampered.json()["valid"] is False
    assert tampered.json()["checks"]["data_prefix_checksum_valid"] is False
    verification_history = client.get(
        f"/admin/accounting-connections/audit-exports/{export_id}/verifications", headers=admin_headers)
    assert [item["valid"] for item in verification_history.json()] == [False, True]


def test_accounting_trends_summarize_sync_failure_alert_response_and_mapping_activity(
    client, admin_headers, monkeypatch,
):
    seed_admin_data(monkeypatch)
    from invoice_match.database import AccountingMappingChangeRecord

    now = datetime.now(timezone.utc)
    alert_created = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1)
    with SessionLocal() as session:
        for run_id, status, error in (("trend-ok", "SUCCEEDED", None),
                                      ("trend-fail-1", "FAILED", "provider timeout"),
                                      ("trend-fail-2", "FAILED", "provider timeout")):
            session.add(MasterDataSyncRunRecord(id=run_id, provider="quickbooks",
                trigger_type="MANUAL", actor="cfo", status=status, counts={}, changes={}, details={},
                error=error, started_at=now, finished_at=now))
        session.add(NotificationOutboxRecord(id="trend-alert", provider="quickbooks", match_id=None,
            dedupe_key="trend-alert-once", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
            payload={"message": "Failure"}, state="DELIVERED", attempt_count=1,
            created_at=alert_created, acknowledged_at=alert_created + timedelta(minutes=30),
            resolved_at=alert_created + timedelta(minutes=50)))
        session.add(AccountingMappingChangeRecord(id="trend-map", provider="quickbooks",
            category="supplier", source_label="ABC Supplies", previous_source_id=None,
            previous_source_label=None, new_source_id="vendor-1", new_source_label="ABC Supplies",
            change_type="ACCEPTED", actor="cfo", created_at=now))
        session.commit()

    response = client.get(
        f"/admin/accounting-connections/trends?start_date={now.date()}&end_date={now.date()}"
        "&provider=quickbooks&bucket=week", headers=admin_headers)
    assert response.status_code == 200
    trends = response.json()
    assert trends["sync_series"][0]["total"] == 3
    assert trends["sync_series"][0]["success_rate_percent"] == 33.33
    assert trends["recurring_failures"][0]["count"] == 2
    assert trends["recurring_failures"][0]["error"] == "provider timeout"
    assert trends["alert_response_series"][0]["avg_ack_minutes"] == 30
    assert trends["alert_response_series"][0]["avg_resolution_minutes"] == 50
    assert trends["mapping_series"][0]["count"] == 1
    assert trends["mapping_series"][0]["by_category"] == {"supplier": 1}
    sync_details = client.get(trends["sync_series"][0]["drilldown_url"], headers=admin_headers)
    failure_details = client.get(trends["recurring_failures"][0]["drilldown_url"], headers=admin_headers)
    alert_details = client.get(trends["alert_response_series"][0]["drilldown_url"], headers=admin_headers)
    mapping_details = client.get(trends["mapping_series"][0]["drilldown_url"], headers=admin_headers)
    assert sync_details.json()["count"] == 3
    assert failure_details.json()["count"] == 2
    assert alert_details.json()["items"][0]["id"] == "trend-alert"
    assert mapping_details.json()["items"][0]["id"] == "trend-map"


def test_configured_trend_anomalies_queue_linked_deduplicated_webhooks(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    from invoice_match.database import AccountingMappingChangeRecord, MasterDataSyncScheduleRecord
    from invoice_match.master_data_sync import evaluate_accounting_trend_alerts

    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        session.add(MasterDataSyncScheduleRecord(id="trend-alert-schedule", provider="quickbooks",
            enabled=False, interval_minutes=1440, stale_after_minutes=2880,
            trend_alerts_enabled=True, trend_window_hours=24, sync_failure_threshold=2,
            alert_response_threshold_minutes=10, alert_response_min_samples=1,
            mapping_change_threshold=1, trend_alert_cooldown_minutes=60))
        for run_id in ("anomaly-failure-1", "anomaly-failure-2"):
            session.add(MasterDataSyncRunRecord(id=run_id, provider="quickbooks",
                trigger_type="SCHEDULED", actor="worker", status="FAILED", counts={}, changes={},
                details={}, error="provider timeout", started_at=now, finished_at=now))
        session.add(NotificationOutboxRecord(id="slow-alert", provider="quickbooks", match_id=None,
            dedupe_key="slow-alert-once", event_type="MASTER_DATA_SYNC_FAILED", channel="webhook",
            payload={"message": "failure"}, state="DELIVERED", attempt_count=1,
            created_at=now - timedelta(minutes=30), acknowledged_at=now - timedelta(minutes=5)))
        session.add(AccountingMappingChangeRecord(id="anomaly-map", provider="quickbooks",
            category="supplier", source_label="ABC", previous_source_id=None,
            previous_source_label=None, new_source_id="vendor-1", new_source_label="ABC",
            change_type="ACCEPTED", actor="cfo", created_at=now))
        session.commit()
        assert evaluate_accounting_trend_alerts(session) == 3
        session.commit()
        assert evaluate_accounting_trend_alerts(session) == 0
        session.commit()
        alerts = session.query(NotificationOutboxRecord).filter_by(
            event_type="MASTER_DATA_TREND_ANOMALY").all()
        assert {item.payload["anomaly_type"] for item in alerts} == {
            "RISING_SYNC_FAILURES", "SLOW_ALERT_RESPONSES", "SUDDEN_MAPPING_CHANGES"}
        assert all("trend_url" in item.payload and "provider=quickbooks" in item.payload["trend_url"]
                   for item in alerts)

    response = client.get("/admin/alerts?state=ALL", headers=admin_headers)
    assert response.status_code == 200
    assert sum(item["event_type"] == "MASTER_DATA_TREND_ANOMALY" for item in response.json()) == 3


def test_admin_can_configure_provider_trend_alert_thresholds(client, admin_headers, monkeypatch):
    seed_admin_data(monkeypatch)
    settings = {"enabled": True, "window_hours": 12, "sync_failure_threshold": 4,
        "alert_response_threshold_minutes": 45, "alert_response_min_samples": 2,
        "mapping_change_threshold": 8, "cooldown_minutes": 180}
    response = client.put("/admin/accounting-connections/quickbooks/trend-alert-settings",
                          headers=admin_headers, json=settings)
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["sync_failure_threshold"] == 4
    status = client.get("/admin/accounting-connections/sync-status", headers=admin_headers)
    provider_status = next(row for row in status.json() if row["provider"] == "quickbooks")
    assert provider_status["trend_alerts_enabled"] is True
    assert provider_status["trend_window_hours"] == 12
    assert provider_status["alert_response_threshold_minutes"] == 45


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
