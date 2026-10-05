from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from sqlalchemy import select

from invoice_match.adapters.filesystem import FilesystemAPAdapter
from invoice_match.adapters.http import GenericHTTPAPAdapter
from invoice_match.database import APOutboxRecord, AuditEvent, SessionLocal
from invoice_match.delivery import deliver_pending
from invoice_match.csv_import import parse_csv_documents
from invoice_match.matcher import match_documents
from invoice_match.rules import ToleranceRules
from invoice_match.schemas import MatchRequest, MatchStatus, SupplierInvoice

ROOT = Path(__file__).resolve().parents[1]


def match_request():
    return json.loads((ROOT / "examples" / "match-request.json").read_text())


def test_filesystem_adapter_fetches_sources_and_writes_idempotent_exports(tmp_path):
    (tmp_path / "purchase_orders").mkdir()
    (tmp_path / "receipts").mkdir()
    (tmp_path / "purchase_orders" / "PO-1.json").write_text(json.dumps({
        "number": "PO-1", "vendor": "ABC", "items": [{"sku": "A100", "qty": 10, "price": 2}],
    }))
    (tmp_path / "receipts" / "PO-1.json").write_text(json.dumps({
        "receipts": [{"number": "GR-1", "items": [{"sku": "A100", "qty": 10}]}],
    }))
    adapter = FilesystemAPAdapter(tmp_path)
    payload = {"match_id": "match-1", "total_amount": "20.00"}

    assert adapter.fetch_purchase_order("PO-1")["number"] == "PO-1"
    assert adapter.fetch_receipts("PO-1")[0]["number"] == "GR-1"
    assert adapter.submit_invoice(payload, idempotency_key="match-1") == "match-1"
    assert adapter.submit_invoice(payload, idempotency_key="match-1") == "match-1"
    assert len(list((tmp_path / "invoices").glob("*.json"))) == 1


def test_generic_http_adapter_uses_documented_routes_and_idempotency_header(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    responses = [{"number": "PO-1"}, {"receipts": []}, {"id": "AP-1"}]

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return Response(responses.pop(0))

    monkeypatch.setattr("invoice_match.adapters.http.urlopen", fake_urlopen)
    adapter = GenericHTTPAPAdapter("https://ap.example.test/api", "secret")
    assert adapter.fetch_purchase_order("PO/1")["number"] == "PO-1"
    assert adapter.fetch_receipts("PO-1") == []
    assert adapter.submit_invoice({"match_id": "m1"}, idempotency_key="m1") == "AP-1"
    assert calls[0][0].full_url.endswith("purchase-orders/PO%2F1")
    assert calls[2][0].get_header("Idempotency-key") == "m1"


def test_delivery_service_records_success_and_retries_failures(clear_database):
    with SessionLocal() as session:
        session.add(APOutboxRecord(match_id="delivery-success", payload={"match_id": "delivery-success"}))
        session.add(APOutboxRecord(match_id="delivery-fail", payload={"match_id": "delivery-fail"}))
        session.commit()

        class Adapter:
            name = "test-ap"

            def submit_invoice(self, payload, *, idempotency_key):
                if idempotency_key == "delivery-fail":
                    raise RuntimeError("downstream unavailable")
                return "AP-123"

        first = deliver_pending(session, Adapter())
        second = deliver_pending(session, Adapter())
        successful = session.get(APOutboxRecord, "delivery-success")
        failed = session.get(APOutboxRecord, "delivery-fail")
        assert first["acknowledged"] == 1 and first["failed"] == 1
        assert second["acknowledged"] == 0 and second["failed"] == 1
        assert successful.state == "ACKNOWLEDGED"
        assert successful.external_reference == "AP-123"
        assert failed.state == "PENDING" and failed.attempt_count == 2
        assert failed.last_error == "downstream unavailable"
        failures = session.scalars(select(AuditEvent).where(AuditEvent.event_type == "AP_EXPORT_FAILED")).all()
        assert len(failures) == 2


def test_multiple_receipts_unit_conversions_and_credit_notes_match():
    rules = ToleranceRules(
        rates_to_base={"USD": 1},
        quantity_units={
            "EA": {"family": "count", "to_base": 1},
            "CASE": {"family": "count", "to_base": 12},
        },
    )
    payload = {
        "po": {"number": "PO-1", "vendor": "ABC", "items": [{"sku": "A", "qty": 2, "unit": "CASE", "price": 120}]},
        "receipt": {"receipts": [
            {"number": "GR-1", "items": [{"sku": "A", "qty": 12, "unit": "EA"}]},
            {"number": "GR-2", "items": [{"sku": "A", "qty": 12, "unit": "EA"}]},
        ]},
        "invoice": {"number": "INV-1", "items": [{"sku": "A", "qty": 24, "unit": "EA", "price": 10}]},
    }
    result = match_documents(MatchRequest.model_validate(payload), rules)
    assert result.status == MatchStatus.MATCHED

    credit = SupplierInvoice.model_validate({
        "number": "CN-1",
        "document_type": "CREDIT_NOTE",
        "credit_note_for": "INV-1",
        "items": [{"sku": "A", "qty": 5, "unit": "EA", "price": 10}],
    })
    credit_request = MatchRequest.model_validate({**payload, "invoice": credit.model_dump(mode="json")})
    credit_result = match_documents(
        credit_request,
        rules,
        previously_invoiced=[SupplierInvoice.model_validate(payload["invoice"])],
    )
    assert credit_result.status == MatchStatus.MATCHED
    assert credit_result.invoice_total < 0
    excessive_credit = credit.model_copy(update={"items": [credit.items[0].model_copy(update={"qty": 25})]})
    excessive_result = match_documents(
        credit_request.model_copy(update={"invoice": excessive_credit}),
        rules,
        previously_invoiced=[SupplierInvoice.model_validate(payload["invoice"])],
    )
    assert "CREDIT_NOTE_EXCEEDS_INVOICED" in {issue.reason for issue in excessive_result.discrepancies}


def test_csv_adapter_import_preserves_receipt_groups_and_units():
    parsed = parse_csv_documents(
        "number,vendor,revision,change_reason,sku,qty,unit,price\nPO-CSV,ABC,2,amended,A,2,CASE,120\n",
        "number,sku,qty,unit\nGR-A,A,12,EA\nGR-B,A,12,EA\n",
        "number,document_type,sku,qty,unit,price\nINV-CSV,INVOICE,A,24,EA,10\n",
    )
    assert parsed.po.revision == 2
    assert len(parsed.receipt.receipts) == 2
    assert parsed.invoice.items[0].unit == "EA"
    result = match_documents(parsed, ToleranceRules(quantity_units={
        "EA": {"family": "count", "to_base": 1},
        "CASE": {"family": "count", "to_base": 12},
    }))
    assert result.status == MatchStatus.MATCHED


def test_different_quantity_families_are_not_compared():
    payload = {
        "po": {"number": "PO-1", "vendor": "ABC", "items": [{"sku": "A", "qty": 1, "unit": "EA", "price": 10}]},
        "receipt": {"items": [{"sku": "A", "qty": 1, "unit": "KG"}]},
        "invoice": {"number": "INV-1", "items": [{"sku": "A", "qty": 1, "unit": "EA", "price": 10}]},
    }
    rules = ToleranceRules(quantity_units={
        "EA": {"family": "count", "to_base": 1},
        "KG": {"family": "mass", "to_base": 1},
    })
    result = match_documents(MatchRequest.model_validate(payload), rules)
    assert "UOM_MISMATCH" in {issue.reason for issue in result.discrepancies}


def test_cumulative_partial_invoices_and_stale_po_revisions(client, integration_headers):
    first = match_request()
    first["po"]["items"][0]["qty"] = 100
    first["receipt"]["items"][0]["qty"] = 100
    first["invoice"]["number"] = "INV-PART-1"
    first["invoice"]["items"][0]["qty"] = 60
    one = client.post(
        "/matches", json=first,
        headers={**integration_headers, "Idempotency-Key": "partial-invoice-first"},
    )
    second = json.loads(json.dumps(first))
    second["invoice"]["number"] = "INV-PART-2"
    second["invoice"]["items"][0]["qty"] = 50
    two = client.post(
        "/matches", json=second,
        headers={**integration_headers, "Idempotency-Key": "partial-invoice-second"},
    )
    assert one.status_code == 201 and one.json()["status"] == "MATCHED"
    assert two.status_code == 201
    assert two.json()["reason"] == "RECEIPT_QUANTITY_MISMATCH"
    assert two.json()["expected"] == "100"
    assert two.json()["invoiced"] == "110"

    amended = match_request()
    amended["po"]["revision"] = 2
    amended["po"]["change_reason"] = "Supplier-approved quantity amendment"
    amended["invoice"]["number"] = "INV-REV-2"
    revision_two = client.post(
        "/matches", json=amended,
        headers={**integration_headers, "Idempotency-Key": "revision-two-invoice"},
    )
    stale = json.loads(json.dumps(amended))
    stale["po"]["revision"] = 1
    stale["po"].pop("change_reason")
    stale["invoice"]["number"] = "INV-REV-1"
    revision_one = client.post(
        "/matches", json=stale,
        headers={**integration_headers, "Idempotency-Key": "revision-one-invoice"},
    )
    assert revision_two.status_code == 201
    assert revision_one.status_code == 409
    assert revision_one.json()["detail"]["code"] == "STALE_PO_REVISION"


def test_email_capture_requires_human_verification_before_matching(
    client, integration_headers, reviewer_headers, viewer_headers
):
    email = b"""From: billing@example.test\nSubject: Supplier invoice\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nInvoice Number: INV-CAPTURE-1\nVendor: ABC Supplies\nCurrency: USD\nITEM: A100 | 100 | 10.00\nTax: 0.00\nTotal: 1000.00\n"""
    captured = client.post(
        "/documents/extract",
        content=email,
        headers={**integration_headers, "Content-Type": "message/rfc822", "X-Filename": "invoice.eml"},
    )
    assert captured.status_code == 201
    document_id = captured.json()["document_id"]
    assert captured.json()["status"] == "REVIEW_REQUIRED"
    assert captured.json()["extracted_fields"]["number"] == "INV-CAPTURE-1"
    queue = client.get("/documents", headers=reviewer_headers)
    assert [item["document_id"] for item in queue.json()] == [document_id]
    duplicate_capture = client.post(
        "/documents/extract",
        content=email,
        headers={**integration_headers, "Content-Type": "message/rfc822", "X-Filename": "duplicate.eml"},
    )
    assert duplicate_capture.status_code == 409
    assert client.post(
        f"/documents/{document_id}/match",
        json={"po": match_request()["po"], "receipt": {"items": [{"sku": "A100", "qty": 100}]}},
        headers={**integration_headers, "Idempotency-Key": "capture-before-review"},
    ).status_code == 409

    source = client.get(f"/documents/{document_id}/source", headers=reviewer_headers)
    assert source.status_code == 200 and source.content == email
    verify_body = {"invoice": {
        "number": "INV-CAPTURE-1", "vendor": "ABC Supplies", "currency": "USD",
        "items": [{"sku": "A100", "qty": 100, "price": 10}],
        "tax_amount": 0, "total_amount": 1000,
    }}
    viewer_verification = client.post(
        f"/documents/{document_id}/verify",
        headers=viewer_headers,
        json=verify_body,
    )
    assert viewer_verification.status_code == 403
    verification = client.post(
        f"/documents/{document_id}/verify",
        headers=reviewer_headers,
        json=verify_body,
    )
    assert verification.status_code == 200
    assert verification.json()["status"] == "VERIFIED"
    matched = client.post(
        f"/documents/{document_id}/match",
        json={"po": match_request()["po"], "receipt": {"items": [{"sku": "A100", "qty": 100}]}},
        headers={**integration_headers, "Idempotency-Key": "capture-after-review"},
    )
    assert matched.status_code == 201
    assert matched.json()["invoice_number"] == "INV-CAPTURE-1"
    audit = client.get(f"/documents/{document_id}/audit", headers=reviewer_headers)
    assert [event["event_type"] for event in audit.json()] == ["DOCUMENT_CAPTURED", "DOCUMENT_VERIFIED"]


def test_pdf_capture_extracts_text_from_pypdf_backend(client, integration_headers, monkeypatch):
    class Page:
        def extract_text(self):
            return "Invoice Number: INV-PDF-1\nVendor: ABC\nCurrency: USD\nITEM: A100 | 1 | 10.00\nTotal: 10.00"

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, _stream, strict=False):
            pass

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
    response = client.post(
        "/documents/extract",
        content=b"fake-pdf-bytes",
        headers={**integration_headers, "Content-Type": "application/pdf", "X-Filename": "invoice.pdf"},
    )
    assert response.status_code == 201
    assert response.json()["extracted_fields"]["number"] == "INV-PDF-1"
    assert response.json()["confidence"]["number"] == 0.98
