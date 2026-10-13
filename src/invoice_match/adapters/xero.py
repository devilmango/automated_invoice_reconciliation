from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import XeroCredentialRecord

_TOKEN_URL = "https://identity.xero.com/connect/token"
_API_URL = "https://api.xero.com/api.xro/2.0/"


class XeroAPIError(RuntimeError):
    """Raised for Xero API or token-refresh failures."""


class XeroAdapter:
    """Create idempotent Xero accounts-payable invoices using refreshable OAuth tokens."""

    name = "xero"

    def __init__(self, *, client_id: str, client_secret: str, tenant_id: str,
                 encryption_key: str, refresh_token: str | None = None,
                 contact_map: dict[str, str] | None = None,
                 item_map: dict[str, str] | None = None,
                 tax_type_map: dict[str, str] | None = None,
                 currency_map: dict[str, str] | None = None,
                 expense_account_code: str | None = None, timeout: float = 20,
                 session_factory: Callable[[], Session] | None = None, opener=urlopen):
        if not all((client_id, client_secret, tenant_id, encryption_key)):
            raise ValueError("Xero requires client ID, client secret, tenant ID, and token encryption key")
        try:
            self.fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("XERO_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        if session_factory is None:
            from ..database import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory
        self.client_id, self.client_secret, self.tenant_id = client_id, client_secret, tenant_id
        self.bootstrap_refresh_token = refresh_token
        self.contact_map = self._normalize_map(contact_map or {})
        self.item_map = self._normalize_map(item_map or {})
        self.tax_type_map = self._normalize_map(tax_type_map or {})
        self.currency_map = self._normalize_map(currency_map or {})
        self.expense_account_code = expense_account_code
        self.timeout, self.opener = timeout, opener

    @classmethod
    def from_environment(cls, *, session_factory=None):
        def read_map(name):
            try:
                value = json.loads(os.getenv(name, "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} must be a JSON object") from exc
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  for k, v in value.items()):
                raise ValueError(f"{name} must map names to Xero identifiers")
            return value
        return cls(
            client_id=os.getenv("XERO_CLIENT_ID", ""),
            client_secret=os.getenv("XERO_CLIENT_SECRET", ""),
            tenant_id=os.getenv("XERO_TENANT_ID", ""),
            encryption_key=os.getenv("XERO_TOKEN_ENCRYPTION_KEY", ""),
            refresh_token=os.getenv("XERO_REFRESH_TOKEN"),
            contact_map=read_map("XERO_CONTACT_ID_MAP"),
            item_map=read_map("XERO_ITEM_CODE_MAP"),
            tax_type_map=read_map("XERO_TAX_TYPE_MAP"),
            currency_map=read_map("XERO_CURRENCY_MAP"),
            expense_account_code=os.getenv("XERO_EXPENSE_ACCOUNT_CODE"),
            timeout=float(os.getenv("XERO_TIMEOUT_SECONDS", "20")),
            session_factory=session_factory,
        )

    @classmethod
    def from_configuration(cls, config: dict, *, session_factory=None):
        return cls(
            client_id=config["client_id"], client_secret=config["client_secret"],
            tenant_id=config["tenant_id"], encryption_key=config["token_encryption_key"],
            refresh_token=config["refresh_token"], contact_map=config.get("contact_map", {}),
            item_map=config.get("item_map", {}), tax_type_map=config.get("tax_type_map", {}),
            currency_map=config.get("currency_map", {}),
            expense_account_code=config.get("expense_account_code"),
            timeout=float(config.get("timeout", 20)), session_factory=session_factory,
        )

    @staticmethod
    def _normalize_map(source):
        return {" ".join(key.casefold().split()): value for key, value in source.items()}

    def _decrypt(self, value):
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise XeroAPIError("Stored Xero token cannot be decrypted; check XERO_TOKEN_ENCRYPTION_KEY") from exc

    def _refresh(self, session, record):
        refresh = self.bootstrap_refresh_token
        if record is not None:
            refresh = self._decrypt(record.encrypted_refresh_token)
        if not refresh:
            raise XeroAPIError("No Xero refresh token is configured")
        import base64
        authorization = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
        request = Request(_TOKEN_URL, data=body, headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        }, method="POST")
        data = self._open_json(request)
        now = datetime.now(timezone.utc)
        if not all(isinstance(data.get(key), str) for key in ("access_token", "refresh_token")):
            raise XeroAPIError("Xero token endpoint returned an incomplete token response")
        if record is None:
            record = XeroCredentialRecord(tenant_id=self.tenant_id,
                encrypted_access_token="", encrypted_refresh_token="",
                access_token_expires_at=now)
            session.add(record)
        record.encrypted_access_token = self.fernet.encrypt(data["access_token"].encode()).decode()
        record.encrypted_refresh_token = self.fernet.encrypt(data["refresh_token"].encode()).decode()
        record.access_token_expires_at = now + timedelta(seconds=int(data.get("expires_in", 1800)))
        record.updated_at = now
        session.flush()
        return data["access_token"]

    def _token(self):
        with self.session_factory() as session:
            record = session.scalar(select(XeroCredentialRecord).where(
                XeroCredentialRecord.tenant_id == self.tenant_id).with_for_update())
            now = datetime.now(timezone.utc)
            if record is not None:
                expires = record.access_token_expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires > now + timedelta(seconds=60):
                    return self._decrypt(record.encrypted_access_token)
            token = self._refresh(session, record)
            session.commit()
            return token

    def _open_json(self, request):
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(1_000_000)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            raise XeroAPIError(f"Xero API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise XeroAPIError(f"Xero request failed: {exc.reason}") from exc
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise XeroAPIError("Xero returned an invalid JSON response") from exc

    def _api(self, path, *, method="GET", data=None, query=None, idempotency_key=None):
        url = _API_URL + path
        if query:
            url += "?" + urlencode(query)
        headers = {"Authorization": f"Bearer {self._token()}", "Xero-tenant-id": self.tenant_id,
                   "Accept": "application/json", "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        payload = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
        request = Request(url, data=payload, headers=headers, method=method)
        return self._open_json(request)

    def _find_invoice(self, invoice_number):
        data = self._api("Invoices", query={"InvoiceNumbers": invoice_number})
        for invoice in data.get("Invoices", []):
            if invoice.get("Type") == "ACCPAY" and invoice.get("InvoiceNumber") == invoice_number:
                return invoice
        return None

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str:
        if payload.get("document_type", "INVOICE") != "INVOICE":
            raise ValueError("Xero export currently supports invoices, not credit notes")
        invoice_number = str(payload.get("supplier_invoice_number") or "").strip()
        supplier = " ".join(str(payload.get("supplier") or "").casefold().split())
        contact_id = self.contact_map.get(supplier)
        if not invoice_number or not contact_id:
            raise ValueError("Xero export requires an invoice number and configured supplier ContactID")
        marker = f"invoice-match-id:{idempotency_key}"
        existing = self._find_invoice(invoice_number)
        if existing:
            if marker not in str(existing.get("Reference") or ""):
                raise ValueError(f"Xero already has an AP invoice numbered {invoice_number!r}")
            return str(existing.get("InvoiceID") or "")
        lines = []
        for line in payload.get("lines", []):
            sku = " ".join(str(line.get("sku") or "").casefold().split())
            item_code = self.item_map.get(sku)
            if not item_code:
                raise ValueError(f"No Xero item-code mapping configured for SKU {line.get('sku')!r}")
            row = {"ItemCode": item_code, "Description": str(line.get("description") or sku),
                   "Quantity": float(Decimal(str(line.get("quantity") or 0))),
                   "UnitAmount": float(Decimal(str(line.get("unit_price") or 0))
                                        * (1 - Decimal(str(line.get("discount_rate") or 0))))}
            tax_code = line.get("tax_code")
            tax_type = self.tax_type_map.get(" ".join(str(tax_code).casefold().split())) if tax_code else None
            if tax_code and not tax_type:
                raise ValueError(f"No Xero tax-type mapping configured for tax code {tax_code!r}")
            if tax_type:
                row["TaxType"] = tax_type
            if self.expense_account_code:
                row["AccountCode"] = self.expense_account_code
            lines.append(row)
        invoice = {
            "Type": "ACCPAY", "Contact": {"ContactID": contact_id},
            "InvoiceNumber": invoice_number,
            "Reference": f"{payload.get('purchase_order_number', '')} {marker}"[:255],
            "CurrencyCode": self.currency_map.get(
                str(payload.get("currency") or "USD").casefold(),
                str(payload.get("currency") or "USD").upper(),
            ),
            "LineAmountTypes": "Exclusive", "LineItems": lines,
        }
        data = self._api("Invoices", method="POST", data={"Invoices": [invoice]},
                         idempotency_key=idempotency_key)
        invoices = data.get("Invoices") or []
        if not invoices or invoices[0].get("HasErrors"):
            raise XeroAPIError(f"Xero did not create invoice {invoice_number!r}: {data}")
        return str(invoices[0].get("InvoiceID") or "")

    def fetch_invoice_status(self, external_reference: str) -> dict:
        data = self._api(f"Invoices/{external_reference}")
        invoices = data.get("Invoices") or []
        invoice = next((item for item in invoices if item.get("Type") == "ACCPAY"), None)
        if invoice is None:
            raise XeroAPIError("Xero returned no accounts-payable invoice for the supplied reference")
        provider_status = str(invoice.get("Status") or "UNKNOWN").upper()
        amount_due = Decimal(str(invoice.get("AmountDue") or 0))
        amount_paid = Decimal(str(invoice.get("AmountPaid") or 0))
        if provider_status in {"VOIDED", "DELETED"}:
            status = "VOIDED"
        elif provider_status == "PAID" or amount_due <= Decimal("0.01"):
            status = "PAID"
        elif amount_paid > 0:
            status = "PARTIALLY_PAID"
        else:
            status = "OPEN"
        return {"status": status, "provider_status": provider_status,
                "total_amount": str(invoice.get("Total")), "amount_paid": str(amount_paid),
                "amount_due": str(amount_due), "currency": invoice.get("CurrencyCode"),
                "document_number": invoice.get("InvoiceNumber")}
