from __future__ import annotations

import json

from invoice_match.cli import app as cli_app
from typer.testing import CliRunner


def test_admin_syncs_procurement_documents_for_integration_use(
    client, admin_headers, integration_headers, reviewer_headers, monkeypatch
):
    watermarks = []

    class ProcurementAdapter:
        name = "test-procurement"

        def list_purchase_orders(self, *, updated_since=None):
            watermarks.append(updated_since)
            return [{
                "number": "PO-SYNC-1",
                "vendor": "Northwind Paper",
                "currency": "USD",
                "items": [{"sku": "PAPER-A4", "qty": 20, "price": 3}],
            }]

        def fetch_receipts(self, po_number):
            assert po_number == "PO-SYNC-1"
            return [{"number": "GR-SYNC-1", "items": [{"sku": "PAPER-A4", "qty": 18}]}]

    monkeypatch.setattr("invoice_match.api.create_adapter", lambda *_args, **_kwargs: ProcurementAdapter())

    denied = client.post("/admin/procurement/sync", headers=reviewer_headers)
    assert denied.status_code == 403

    sync = client.post("/admin/procurement/sync", headers=admin_headers)
    assert sync.status_code == 200
    assert sync.json()["synced"] == 1

    fetched = client.get(
        "/integrations/procurement/purchase-orders/PO-SYNC-1", headers=integration_headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["purchase_order"]["vendor"] == "Northwind Paper"
    assert fetched.json()["receipts"][0]["number"] == "GR-SYNC-1"
    second_sync = client.post("/admin/procurement/sync", headers=admin_headers)
    assert second_sync.status_code == 200
    assert second_sync.json()["updated_since"] is not None
    assert watermarks[1] is not None


def test_partial_procurement_failure_does_not_advance_checkpoint(
    client, admin_headers, monkeypatch
):
    class ProcurementAdapter:
        name = "partial-procurement"

        def __init__(self):
            self.fail_bad_po = True
            self.watermarks = []

        def list_purchase_orders(self, *, updated_since=None):
            self.watermarks.append(updated_since)
            return [
                {"number": "PO-GOOD", "vendor": "Northwind", "items": [{"sku": "A", "qty": 1}]},
                {"number": "PO-BAD", "vendor": "Northwind", "items": [{"sku": "B", "qty": 1}]},
            ]

        def fetch_receipts(self, po_number):
            if po_number == "PO-BAD" and self.fail_bad_po:
                raise RuntimeError("receipt service unavailable")
            return []

    adapter = ProcurementAdapter()
    monkeypatch.setattr("invoice_match.api.create_adapter", lambda *_args, **_kwargs: adapter)
    partial = client.post("/admin/procurement/sync", headers=admin_headers)
    assert partial.status_code == 200
    assert partial.json()["synced"] == 1
    assert partial.json()["failed"] == 1
    assert partial.json()["last_successful_at"] is None

    adapter.fail_bad_po = False
    recovered = client.post("/admin/procurement/sync", headers=admin_headers)
    assert recovered.status_code == 200
    assert recovered.json()["synced"] == 2
    assert recovered.json()["last_successful_at"] is not None
    assert adapter.watermarks == [None, None]


def test_procurement_sync_command_can_run_as_a_scheduled_job(
    client, integration_headers, monkeypatch
):
    class ProcurementAdapter:
        name = "test-procurement"

        def list_purchase_orders(self, *, updated_since=None):
            return [{
                "number": "PO-CRON-1",
                "vendor": "Northwind Paper",
                "items": [{"sku": "PAPER-A4", "qty": 10, "price": 2}],
            }]

        def fetch_receipts(self, _po_number):
            return []

    monkeypatch.setattr("invoice_match.adapters.create_adapter", lambda *_args, **_kwargs: ProcurementAdapter())
    result = CliRunner().invoke(cli_app, ["procurement-sync"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["synced"] == 1
    fetched = client.get(
        "/integrations/procurement/purchase-orders/PO-CRON-1", headers=integration_headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["receipts"] == []


def test_capture_inbox_submits_and_archives_invoice_files(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    invoice = inbox / "march-invoice.txt"
    invoice.write_text("Invoice No: INV-MARCH-7\nVendor: Northwind Paper\n")
    calls = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"document_id": "capture-7"}).encode()

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return Response()

    monkeypatch.setattr("invoice_match.cli.urlopen", fake_urlopen)
    result = CliRunner().invoke(cli_app, [
        "capture-inbox", "--directory", str(inbox), "--api-url", "http://localhost:8000",
        "--token", "integration-secret",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["captured"] == 1
    assert calls[0][0].full_url == "http://localhost:8000/documents/extract"
    assert calls[0][0].get_header("X-filename") == "march-invoice.txt"
    assert not invoice.exists()
    assert (inbox / "processed" / invoice.name).exists()


def test_capture_inbox_keeps_transiently_failed_invoice_for_retry(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    invoice = inbox / "failed.pdf"
    invoice.write_bytes(b"invoice bytes")

    def fail_urlopen(_request, timeout):
        from urllib.error import HTTPError

        raise HTTPError("http://localhost/documents/extract", 503, "Unavailable", {}, None)

    monkeypatch.setattr("invoice_match.cli.urlopen", fail_urlopen)
    result = CliRunner().invoke(cli_app, [
        "capture-inbox", "--directory", str(inbox), "--api-url", "http://localhost:8000",
        "--token", "integration-secret",
    ])

    assert result.exit_code == 1
    assert invoice.exists()
    assert not (inbox / "processed" / invoice.name).exists()
