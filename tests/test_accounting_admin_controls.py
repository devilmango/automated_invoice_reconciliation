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
