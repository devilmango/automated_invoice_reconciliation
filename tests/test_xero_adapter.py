from __future__ import annotations

import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import select

from invoice_match.adapters.xero import XeroAdapter
from invoice_match.database import ProcurementRecord, SessionLocal, XeroCredentialRecord


def test_company_scoped_sessions_hide_other_companies(clear_database):
    with SessionLocal() as session:
        session.info["company_id"] = "north"
        session.add(ProcurementRecord(po_number="PO-1", purchase_order={"number": "PO-1"},
                                      receipts=[], source_adapter="test"))
        session.commit()
    with SessionLocal() as session:
        session.info["company_id"] = "south"
        assert session.scalars(select(ProcurementRecord)).all() == []
        session.add(ProcurementRecord(po_number="PO-1", purchase_order={"number": "PO-1", "vendor": "South"},
                                      receipts=[], source_adapter="test"))
        session.commit()
    with SessionLocal() as session:
        session.info["company_id"] = "north"
        records = session.scalars(select(ProcurementRecord)).all()
        assert len(records) == 1 and records[0].purchase_order["number"] == "PO-1"
    with SessionLocal() as session:
        session.info["company_id"] = "south"
        records = session.scalars(select(ProcurementRecord)).all()
        assert len(records) == 1 and records[0].purchase_order["vendor"] == "South"


def test_xero_refreshes_tokens_and_submits_idempotent_ap_invoice(clear_database):
    calls = []

    class Response:
        def __init__(self, payload):
            self.body = json.dumps(payload).encode()
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return self.body

    def opener(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/connect/token"):
            return Response({"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 1800})
        if request.method == "GET":
            return Response({"Invoices": []})
        return Response({"Invoices": [{"InvoiceID": "xero-invoice-42", "HasErrors": False}]})

    adapter = XeroAdapter(
        client_id="client", client_secret="secret", tenant_id="tenant-1",
        encryption_key=Fernet.generate_key().decode(), refresh_token="refresh-old",
        contact_map={"ABC Supplies": "contact-1"}, item_map={"A100": "A100"},
        tax_type_map={"STANDARD": "OUTPUT2"}, session_factory=SessionLocal, opener=opener,
    )
    reference = adapter.submit_invoice({
        "document_type": "INVOICE", "supplier_invoice_number": "INV-42",
        "supplier": "ABC Supplies", "purchase_order_number": "PO-42", "currency": "USD",
        "lines": [{"sku": "A100", "description": "Paper", "quantity": "2",
                   "unit_price": "5", "discount_rate": "0", "tax_code": "STANDARD"}],
    }, idempotency_key="match-42")
    assert reference == "xero-invoice-42"
    assert calls[1].get_header("Xero-tenant-id") == "tenant-1"
    assert calls[2].get_header("Idempotency-key") == "match-42"
    assert json.loads(calls[2].data)["Invoices"][0]["Type"] == "ACCPAY"
    with SessionLocal() as session:
        credential = session.scalar(select(XeroCredentialRecord))
        assert credential is not None
        assert credential.encrypted_refresh_token != "refresh-new"


def test_api_scopes_match_reads_and_idempotency_to_token_company(client, monkeypatch):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    request_body = json.loads((root / "examples" / "match-request.json").read_text())
    monkeypatch.setenv("INTEGRATION_TOKENS", json.dumps({
        "acme": {"token": "integration-acme-token-secret-000000001", "company_id": "acme"},
        "northwind": {"token": "integration-northwind-token-secret-001", "company_id": "northwind"},
    }))
    monkeypatch.setenv("REVIEWER_TOKENS", json.dumps({
        "acme-reviewer": {"token": "reviewer-acme-token-secret-0000000001",
                           "roles": ["reviewer"], "company_id": "acme"},
        "northwind-reviewer": {"token": "reviewer-northwind-token-secret-001",
                                "roles": ["reviewer"], "company_id": "northwind"},
    }))
    created = []
    for token in ("integration-acme-token-secret-000000001",
                  "integration-northwind-token-secret-001"):
        response = client.post("/matches", json=request_body, headers={
            "Authorization": f"Bearer {token}", "Idempotency-Key": "shared-company-key",
        })
        assert response.status_code == 201
        created.append(response.json()["match_id"])
    assert created[0] != created[1]
    assert client.get(f"/matches/{created[0]}", headers={
        "Authorization": "Bearer reviewer-acme-token-secret-0000000001",
    }).status_code == 200
    assert client.get(f"/matches/{created[0]}", headers={
        "Authorization": "Bearer reviewer-northwind-token-secret-001",
    }).status_code == 404
