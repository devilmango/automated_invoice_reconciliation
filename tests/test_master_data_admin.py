from __future__ import annotations

from datetime import datetime, timedelta, timezone

from invoice_match.accounting_connections import encrypt_configuration
from invoice_match.database import (
    AccountingConnectionRecord,
    AccountingMasterDataRecord,
    MasterDataSyncScheduleRecord,
    MatchRecord,
    SessionLocal,
)


def _connection(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    config = {
        "client_id": "client", "client_secret": "secret", "refresh_token": "refresh",
        "token_encryption_key": "provider-key", "realm_id": "12345", "vendor_map": {},
        "item_map": {}, "tax_code_map": {},
    }
    with SessionLocal() as session:
        session.add(AccountingConnectionRecord(
            id="admin-ui-connection", provider="quickbooks",
            encrypted_config=encrypt_configuration(config), enabled=True, updated_by="admin",
        ))
        session.add(AccountingMasterDataRecord(
            id="admin-ui-vendor", provider="quickbooks", category="supplier",
            source_id="vendor-1", code="ABC", name="ABC Supplies", active=True,
            source_data={}, synced_at=datetime.now(timezone.utc),
        ))
        session.add(MatchRecord(
            id="admin-ui-match", status="MATCHED", approval_status="PENDING",
            po_number="PO-1", invoice_number="INV-1", vendor="ABC Supplies", vendor_key="abc supplies",
            approval_policy="default", required_role="approver", escalation_role="admin",
            approvals_required=1, approvals_received=0, rule_digest="", rule_snapshot={},
            input_data={"invoice": {"vendor": "ABC Supplies", "items": []}}, result_data={},
        ))
        session.commit()


def test_admin_can_accept_mapping_suggestion_and_audit_applied_mapping(client, admin_headers, monkeypatch):
    _connection(monkeypatch)

    suggestions = client.get("/admin/accounting-connections/quickbooks/mapping-suggestions", headers=admin_headers)
    assert suggestions.status_code == 200
    suggestion = next(item for item in suggestions.json() if item["category"] == "supplier")
    assert suggestion["review_status"] == "PENDING"

    accepted = client.post("/admin/accounting-connections/quickbooks/mapping-suggestions/review",
        headers=admin_headers, json={"category": "supplier", "source_label": "ABC Supplies",
                                     "suggested_source_id": "vendor-1", "decision": "ACCEPT"})
    assert accepted.status_code == 200
    mappings = client.get("/admin/accounting-connections/quickbooks/mappings", headers=admin_headers).json()
    assert mappings["mappings"]["supplier"] == {"ABC Supplies": "vendor-1"}
    events = client.get("/admin/accounting-connections/events", headers=admin_headers).json()
    audit = next(event for event in events if event["event_type"] == "ACCOUNTING_MAPPING_SUGGESTION_ACCEPTED")
    assert audit["actor"] == "cfo"
    assert audit["details"]["source_label"] == "ABC Supplies"
    assert audit["details"]["suggested_source_id"] == "vendor-1"
    assert audit["details"]["mapping_change"] == {"before": None, "after": "vendor-1"}
    revisions = client.get("/admin/accounting-connections/quickbooks/mapping-history", headers=admin_headers)
    assert any(item["change_type"] == "SUGGESTION_ACCEPTED" for item in revisions.json())


def test_admin_can_dismiss_mapping_suggestion_and_it_stays_dismissed(client, admin_headers, monkeypatch):
    _connection(monkeypatch)

    dismissed = client.post("/admin/accounting-connections/quickbooks/mapping-suggestions/review",
        headers=admin_headers, json={"category": "supplier", "source_label": "ABC Supplies",
                                     "suggested_source_id": "vendor-1", "decision": "DISMISS"})
    assert dismissed.status_code == 200
    suggestions = client.get("/admin/accounting-connections/quickbooks/mapping-suggestions", headers=admin_headers)
    suggestion = next(item for item in suggestions.json() if item["category"] == "supplier")
    assert suggestion["review_status"] == "DISMISSED"
    assert suggestion["reviewed_by"] == "cfo"


def test_admin_can_schedule_and_monitor_provider_master_data_sync(client, admin_headers, monkeypatch):
    _connection(monkeypatch)
    now = datetime.now(timezone.utc)
    schedule = client.put("/admin/accounting-connections/quickbooks/sync-schedule", headers=admin_headers,
        json={"enabled": True, "interval_minutes": 60, "stale_after_minutes": 90})
    assert schedule.status_code == 200
    status = client.get("/admin/accounting-connections/sync-status", headers=admin_headers).json()
    provider = next(item for item in status if item["provider"] == "quickbooks")
    assert provider["sync_status"] == "NEVER_SYNCED"
    assert provider["interval_minutes"] == 60

    from invoice_match.database import MasterDataSyncScheduleRecord
    with SessionLocal() as session:
        row = session.query(MasterDataSyncScheduleRecord).filter_by(provider="quickbooks").one()
        row.last_success_at = now - timedelta(hours=3)
        row.next_run_at = now + timedelta(minutes=30)
        session.commit()
    status = client.get("/admin/accounting-connections/sync-status", headers=admin_headers).json()
    provider = next(item for item in status if item["provider"] == "quickbooks")
    assert provider["sync_status"] == "STALE"
    assert provider["last_success_at"] is not None


def test_worker_runs_due_master_data_sync_and_schedules_next_run(monkeypatch):
    _connection(monkeypatch)
    from invoice_match.api import _apply_master_data_sync
    from invoice_match.master_data_sync import sync_due_master_data

    with SessionLocal() as session:
        schedule = MasterDataSyncScheduleRecord(id="due-sync", provider="quickbooks", enabled=True,
            interval_minutes=45, stale_after_minutes=90, next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        session.add(schedule)
        session.commit()

        class Adapter:
            name = "quickbooks"

            def fetch_master_data(self):
                return {"supplier": [{"source_id": "vendor-2", "name": "New Vendor", "active": True}]}

        result = sync_due_master_data(session, company_id="default",
            adapter_factory=lambda provider, company: Adapter(), apply_sync=_apply_master_data_sync,
            actor="worker", limit=10)
        session.refresh(schedule)
        assert result == {"synced": 1, "failed": 0}
        assert schedule.last_success_at is not None
        assert schedule.next_run_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        assert schedule.last_error is None


def test_worker_keeps_failed_master_data_sync_visible_and_retries(monkeypatch):
    _connection(monkeypatch)
    from invoice_match.api import _apply_master_data_sync
    from invoice_match.master_data_sync import sync_due_master_data

    with SessionLocal() as session:
        schedule = MasterDataSyncScheduleRecord(id="failed-sync", provider="quickbooks", enabled=True,
            interval_minutes=45, stale_after_minutes=90, next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        session.add(schedule)
        session.commit()

        class Adapter:
            name = "quickbooks"

            def fetch_master_data(self):
                raise RuntimeError("provider temporarily unavailable")

        result = sync_due_master_data(session, company_id="default",
            adapter_factory=lambda provider, company: Adapter(), apply_sync=_apply_master_data_sync,
            actor="worker", limit=10)
        session.refresh(schedule)
        assert result == {"synced": 0, "failed": 1}
        assert schedule.last_error == "provider temporarily unavailable"
        assert schedule.next_run_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        assert schedule.last_success_at is None
