from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from invoice_match.adapters.quickbooks import QuickBooksAPIError, QuickBooksOnlineAdapter, _TOKEN_URL as QBO_TOKEN_URL
from invoice_match.adapters.xero import XeroAPIError, XeroAdapter, _TOKEN_URL as XERO_TOKEN_URL
from invoice_match.database import QuickBooksCredentialRecord, SessionLocal, XeroCredentialRecord

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def fixture(provider: str, name: str) -> dict:
    return json.loads((FIXTURES / provider / name).read_text())


class Response:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def unauthorized(url: str) -> HTTPError:
    return HTTPError(url, 401, "Unauthorized", {}, BytesIO(b"expired token"))


def test_quickbooks_contract_refreshes_once_and_preserves_write_idempotency(clear_database):
    key = Fernet.generate_key().decode()
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url == QBO_TOKEN_URL:
            refresh = parse_qs(request.data.decode())["refresh_token"][0]
            return Response(fixture("quickbooks", "token-v1.json" if refresh == "bootstrap" else "token-v2.json"))
        if request.get_header("Authorization") == "Bearer qbo-access-v1":
            raise unauthorized(request.full_url)
        return Response(fixture("quickbooks", "bill.json"))

    adapter = QuickBooksOnlineAdapter(client_id="client", client_secret="secret", realm_id="100000000000001",
        refresh_token="bootstrap", encryption_key=key, session_factory=SessionLocal, opener=opener)
    result = adapter._request("POST", "bill", params={"requestid": "invoice-match-1"},
                              body={"PrivateNote": "invoice-match-id:invoice-match-1"})

    api_calls = [call for call in calls if call.full_url != QBO_TOKEN_URL]
    assert result == fixture("quickbooks", "bill.json")
    assert len(api_calls) == 2
    assert all(parse_qs(urlparse(call.full_url).query)["requestid"] == ["invoice-match-1"]
               for call in api_calls)
    assert [call.get_header("Authorization") for call in api_calls] == [
        "Bearer qbo-access-v1", "Bearer qbo-access-v2"]
    with SessionLocal() as session:
        credentials = session.scalar(select(QuickBooksCredentialRecord).where(
            QuickBooksCredentialRecord.realm_id == "100000000000001"))
        assert Fernet(key.encode()).decrypt(credentials.encrypted_refresh_token.encode()).decode() == "qbo-refresh-v2"


def test_xero_contract_refreshes_once_and_preserves_write_idempotency(clear_database):
    key = Fernet.generate_key().decode()
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.full_url == XERO_TOKEN_URL:
            refresh = parse_qs(request.data.decode())["refresh_token"][0]
            return Response(fixture("xero", "token-v1.json" if refresh == "bootstrap" else "token-v2.json"))
        if request.get_header("Authorization") == "Bearer xero-access-v1":
            raise unauthorized(request.full_url)
        return Response(fixture("xero", "invoice.json"))

    adapter = XeroAdapter(client_id="client", client_secret="secret", tenant_id="tenant-1",
        encryption_key=key, refresh_token="bootstrap", session_factory=SessionLocal, opener=opener)
    result = adapter._api("Invoices", method="POST", data={"Invoices": [{"InvoiceNumber": "INV-1"}]},
                          idempotency_key="invoice-match-1")

    api_calls = [call for call in calls if call.full_url != XERO_TOKEN_URL]
    assert result == fixture("xero", "invoice.json")
    assert len(api_calls) == 2
    assert [call.get_header("Authorization") for call in api_calls] == [
        "Bearer xero-access-v1", "Bearer xero-access-v2"]
    assert [call.get_header("Idempotency-key") for call in api_calls] == [
        "invoice-match-1", "invoice-match-1"]
    with SessionLocal() as session:
        credentials = session.scalar(select(XeroCredentialRecord).where(
            XeroCredentialRecord.tenant_id == "tenant-1"))
        assert Fernet(key.encode()).decrypt(credentials.encrypted_refresh_token.encode()).decode() == "xero-refresh-v2"


def test_xero_contract_reuses_provider_document_for_same_idempotency_marker(clear_database):
    adapter = XeroAdapter(client_id="client", client_secret="secret", tenant_id="tenant-idem",
        encryption_key=Fernet.generate_key().decode(), refresh_token="bootstrap",
        contact_map={"ABC Supplies": "contact-1"}, item_map={"A100": "A100"},
        session_factory=SessionLocal)
    calls = []

    def api(path, *, method="GET", data=None, query=None, idempotency_key=None):
        calls.append((path, method, idempotency_key))
        return {"Invoices": [{"Type": "ACCPAY", "InvoiceNumber": "INV-RETRY",
            "InvoiceID": "xero-invoice-existing", "Reference": "invoice-match-id:match-retry"}]}

    adapter._api = api
    result = adapter.submit_invoice({"document_type": "INVOICE", "supplier_invoice_number": "INV-RETRY",
        "supplier": "ABC Supplies", "currency": "USD", "lines": [{"sku": "A100",
        "quantity": "1", "unit_price": "10"}]}, idempotency_key="match-retry")

    assert result == "xero-invoice-existing"
    assert calls == [("Invoices", "GET", None)]


@pytest.mark.parametrize("provider", ["quickbooks", "xero"])
def test_provider_contract_exposes_rate_limit_and_retry_after(provider, clear_database):
    key = Fernet.generate_key().decode()

    def opener(request, timeout):
        if request.full_url in (QBO_TOKEN_URL, XERO_TOKEN_URL):
            return Response(fixture(provider, "token-v1.json"))
        raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "30"},
                        BytesIO(json.dumps(fixture(provider, "rate-limit.json")).encode()))

    if provider == "quickbooks":
        adapter = QuickBooksOnlineAdapter(client_id="client", client_secret="secret", realm_id="100000000000002",
            refresh_token="bootstrap", encryption_key=key, session_factory=SessionLocal, opener=opener)
        with pytest.raises(QuickBooksAPIError) as raised:
            adapter._request("GET", "bill")
    else:
        adapter = XeroAdapter(client_id="client", client_secret="secret", tenant_id="tenant-2",
            encryption_key=key, refresh_token="bootstrap", session_factory=SessionLocal, opener=opener)
        with pytest.raises(XeroAPIError) as raised:
            adapter._api("Invoices")

    assert raised.value.status == 429
    assert raised.value.retry_after == "30"
    assert "429" in str(raised.value)
