from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from cryptography.fernet import Fernet
from sqlalchemy import select

from invoice_match.adapters.quickbooks import QuickBooksOnlineAdapter, _TOKEN_URL
from invoice_match.database import QuickBooksCredentialRecord, SessionLocal


def make_adapter(*, opener=None):
    options = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "realm_id": "123456789012345",
        "refresh_token": "initial-refresh-token",
        "encryption_key": Fernet.generate_key().decode("ascii"),
        "vendor_map": {"ABC Supplies": "vendor-7"},
        "item_map": {"A100": "item-42"},
        "tax_code_map": {"STANDARD": "tax-3"},
        "session_factory": SessionLocal,
    }
    if opener is not None:
        options["opener"] = opener
    return QuickBooksOnlineAdapter(**options)


def payable_payload():
    return {
        "match_id": "match-123",
        "purchase_order_number": "PO-19",
        "supplier_invoice_number": "INV-58",
        "document_type": "INVOICE",
        "supplier": "ABC Supplies",
        "currency": "USD",
        "total_amount": "48.60",
        "freight_amount": "0.00",
        "discount_amount": "0.00",
        "lines": [{
            "sku": "A100", "description": "Widgets", "quantity": "2.00",
            "unit_price": "25.00", "discount_rate": "0.10",
            "line_net": "45.00", "line_tax": "3.60", "tax_code": "STANDARD",
        }],
    }


def test_quickbooks_bill_uses_configured_mappings_and_stable_request_id():
    adapter = make_adapter()
    adapter._find_bill = lambda invoice_number: None
    requests = []

    def request(method, path, *, params=None, body=None):
        requests.append((method, path, params, body))
        return {"Bill": {"Id": "qbo-bill-900", "TotalAmt": 48.6}}

    adapter._request = request
    result = adapter.submit_invoice(payable_payload(), idempotency_key="match-123")

    assert result == "qbo-bill-900"
    method, path, params, body = requests[0]
    assert (method, path) == ("POST", "bill")
    assert params == {"requestid": "match-123"}
    assert body["VendorRef"] == {"value": "vendor-7"}
    assert body["Line"][0]["ItemBasedExpenseLineDetail"]["ItemRef"] == {"value": "item-42"}
    assert body["Line"][0]["ItemBasedExpenseLineDetail"]["TaxCodeRef"] == {"value": "tax-3"}
    assert "invoice-match-id:match-123" in body["PrivateNote"]


def test_quickbooks_retry_returns_existing_bill_with_own_idempotency_marker():
    adapter = make_adapter()
    adapter._find_bill = lambda invoice_number: {
        "Id": "qbo-bill-existing", "PrivateNote": "invoice-match-id:match-123", "TotalAmt": 48.6
    }
    adapter._request = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("duplicate bill must not be posted")
    )

    assert adapter.submit_invoice(payable_payload(), idempotency_key="match-123") == "qbo-bill-existing"


def test_quickbooks_duplicate_invoice_from_another_match_is_rejected():
    adapter = make_adapter()
    adapter._find_bill = lambda invoice_number: {
        "Id": "qbo-bill-existing", "PrivateNote": "invoice-match-id:other-match"
    }
    try:
        adapter.submit_invoice(payable_payload(), idempotency_key="match-123")
    except ValueError as exc:
        assert "already has a Bill" in str(exc)
    else:
        raise AssertionError("provider duplicate should be rejected")


def test_quickbooks_rejects_unmapped_tax_and_unsupported_credit_notes():
    adapter = make_adapter()
    adapter._find_bill = lambda invoice_number: None
    missing_tax = payable_payload()
    missing_tax["lines"][0]["tax_code"] = "UNKNOWN"
    try:
        adapter.submit_invoice(missing_tax, idempotency_key="match-123")
    except ValueError as exc:
        assert "tax-code mapping" in str(exc)
    else:
        raise AssertionError("unmapped tax code should be rejected")

    credit_note = payable_payload()
    credit_note["document_type"] = "CREDIT_NOTE"
    try:
        adapter.submit_invoice(credit_note, idempotency_key="match-credit")
    except ValueError as exc:
        assert "credit notes" in str(exc)
    else:
        raise AssertionError("credit note support should fail explicitly")


class FakeResponse:
    def __init__(self, data: dict):
        self.data = json.dumps(data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, *args):
        return self.data


def test_oauth_refresh_tokens_are_encrypted_and_rotation_is_persisted():
    key = Fernet.generate_key().decode("ascii")
    received_bodies = []

    def opener(request, timeout):
        assert request.full_url == _TOKEN_URL
        received_bodies.append(parse_qs(request.data.decode("utf-8")))
        return FakeResponse({"access_token": "access-one", "refresh_token": "refresh-one",
                             "expires_in": 3600})

    adapter = QuickBooksOnlineAdapter(
        client_id="client-id", client_secret="client-secret", realm_id="123456789012346",
        refresh_token="refresh-bootstrap", encryption_key=key,
        session_factory=SessionLocal, opener=opener,
    )
    assert adapter._access_token() == "access-one"
    with SessionLocal() as session:
        record = session.get(QuickBooksCredentialRecord, "123456789012346")
        assert record.encrypted_access_token != "access-one"
        assert record.encrypted_refresh_token != "refresh-one"
        assert Fernet(key.encode()).decrypt(record.encrypted_refresh_token.encode()).decode() == "refresh-one"
        record.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    def rotated_opener(request, timeout):
        received_bodies.append(parse_qs(request.data.decode("utf-8")))
        return FakeResponse({"access_token": "access-two", "refresh_token": "refresh-two",
                             "expires_in": 3600})

    adapter.opener = rotated_opener
    assert adapter._access_token() == "access-two"
    assert received_bodies[0]["refresh_token"] == ["refresh-bootstrap"]
    assert received_bodies[1]["refresh_token"] == ["refresh-one"]
    with SessionLocal() as session:
        record = session.scalar(select(QuickBooksCredentialRecord).where(
            QuickBooksCredentialRecord.realm_id == "123456789012346"
        ))
        assert Fernet(key.encode()).decrypt(record.encrypted_refresh_token.encode()).decode() == "refresh-two"
