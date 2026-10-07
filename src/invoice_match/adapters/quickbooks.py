from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import QuickBooksCredentialRecord

_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_PRODUCTION_API = "https://quickbooks.api.intuit.com"
_SANDBOX_API = "https://sandbox-quickbooks.api.intuit.com"


class QuickBooksAPIError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"QuickBooks API returned HTTP {status}: {detail}")
        self.status = status


class QuickBooksOnlineAdapter:
    """QuickBooks Online Bill export using encrypted, refreshable OAuth tokens.

    QuickBooks identifiers are resolved through explicit supplier, item, tax-code, and
    account maps. The adapter is a payables-delivery adapter; it does not fetch receipt
    documents, which are not represented consistently across QBO workflows.
    """

    name = "quickbooks"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        realm_id: str,
        encryption_key: str,
        refresh_token: str | None = None,
        environment: str = "sandbox",
        vendor_map: dict[str, str] | None = None,
        item_map: dict[str, str] | None = None,
        tax_code_map: dict[str, str] | None = None,
        ap_account_id: str | None = None,
        freight_account_id: str | None = None,
        discount_account_id: str | None = None,
        timeout: float = 20.0,
        session_factory: Callable[[], Session] | None = None,
        opener=urlopen,
    ):
        if not all((client_id, client_secret, realm_id, encryption_key)):
            raise ValueError("QuickBooks requires client ID, client secret, realm ID, and token encryption key")
        if not realm_id.isdigit():
            raise ValueError("QBO_REALM_ID must be the numeric company ID returned by Intuit OAuth")
        if environment not in {"sandbox", "production"}:
            raise ValueError("QBO_ENVIRONMENT must be 'sandbox' or 'production'")
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("QBO_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
        if session_factory is None:
            from ..database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self.client_id = client_id
        self.client_secret = client_secret
        self.realm_id = realm_id
        self.bootstrap_refresh_token = refresh_token
        self.base_url = _PRODUCTION_API if environment == "production" else _SANDBOX_API
        self.vendor_map = self._normalize_map(vendor_map or {})
        self.item_map = self._normalize_map(item_map or {})
        self.tax_code_map = self._normalize_map(tax_code_map or {})
        self.ap_account_id = ap_account_id
        self.freight_account_id = freight_account_id
        self.discount_account_id = discount_account_id
        self.timeout = timeout
        self.opener = opener
        minor_version = os.getenv("QBO_MINOR_VERSION")
        self.minor_version = minor_version.strip() if minor_version else None

    @classmethod
    def from_environment(cls, *, session_factory=None) -> "QuickBooksOnlineAdapter":
        def read_map(name: str) -> dict[str, str]:
            raw = os.getenv(name, "{}")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} must be a JSON object") from exc
            if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  for k, v in value.items()):
                raise ValueError(f"{name} must map names to QuickBooks IDs")
            return value

        return cls(
            client_id=os.getenv("QBO_CLIENT_ID", ""),
            client_secret=os.getenv("QBO_CLIENT_SECRET", ""),
            realm_id=os.getenv("QBO_REALM_ID", ""),
            encryption_key=os.getenv("QBO_TOKEN_ENCRYPTION_KEY", ""),
            refresh_token=os.getenv("QBO_REFRESH_TOKEN"),
            environment=os.getenv("QBO_ENVIRONMENT", "sandbox").strip().lower(),
            vendor_map=read_map("QBO_VENDOR_ID_MAP"),
            item_map=read_map("QBO_ITEM_ID_MAP"),
            tax_code_map=read_map("QBO_TAX_CODE_MAP"),
            ap_account_id=os.getenv("QBO_AP_ACCOUNT_ID"),
            freight_account_id=os.getenv("QBO_FREIGHT_ACCOUNT_ID"),
            discount_account_id=os.getenv("QBO_DISCOUNT_ACCOUNT_ID"),
            timeout=float(os.getenv("QBO_TIMEOUT_SECONDS", "20")),
            session_factory=session_factory,
        )

    @staticmethod
    def _normalize_map(source: dict[str, str]) -> dict[str, str]:
        return {" ".join(key.casefold().split()): value for key, value in source.items()}

    @staticmethod
    def _decimal_number(value) -> float:
        return float(Decimal(str(value)))

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str:
        if payload.get("document_type", "INVOICE") != "INVOICE":
            raise ValueError("QuickBooks Bill export currently supports invoices, not credit notes")
        invoice_number = str(payload.get("supplier_invoice_number") or "").strip()
        supplier = " ".join(str(payload.get("supplier") or "").casefold().split())
        vendor_id = self.vendor_map.get(supplier)
        if not invoice_number:
            raise ValueError("QuickBooks export requires a supplier invoice number")
        if not vendor_id:
            raise ValueError(f"No QBO vendor mapping configured for supplier {payload.get('supplier')!r}")

        marker = f"invoice-match-id:{idempotency_key}"
        existing = self._find_bill(invoice_number)
        if existing is not None:
            note = str(existing.get("PrivateNote") or "")
            if marker in note:
                self._validate_bill_total(existing, payload)
                return str(existing["Id"])
            raise ValueError(f"QuickBooks already has a Bill with DocNumber {invoice_number!r}")

        lines = []
        for line in payload.get("lines", []):
            sku_key = " ".join(str(line.get("sku", "")).casefold().split())
            item_id = self.item_map.get(sku_key)
            if not item_id:
                raise ValueError(f"No QBO item mapping configured for SKU {line.get('sku')!r}")
            tax_code = line.get("tax_code")
            tax_id = self.tax_code_map.get(" ".join(str(tax_code).casefold().split())) if tax_code else None
            tax_amount = Decimal(str(line.get("line_tax") or "0"))
            if tax_amount and not tax_id:
                raise ValueError(f"No QBO tax-code mapping configured for tax code {tax_code!r}")
            amount = Decimal(str(line.get("line_net") or "0"))
            detail = {
                "ItemRef": {"value": item_id},
                "Qty": self._decimal_number(line.get("quantity", 0)),
                "UnitPrice": self._decimal_number(line.get("unit_price", 0))
                              * (1 - self._decimal_number(line.get("discount_rate", 0))),
            }
            if tax_id:
                detail["TaxCodeRef"] = {"value": tax_id}
            bill_line = {
                "DetailType": "ItemBasedExpenseLineDetail",
                "Amount": self._decimal_number(amount),
                "ItemBasedExpenseLineDetail": detail,
            }
            if line.get("description"):
                bill_line["Description"] = str(line["description"])
            lines.append(bill_line)

        expected_tax = Decimal(str(payload.get("tax_amount") or "0"))
        if expected_tax and not any(line.get("ItemBasedExpenseLineDetail", {}).get("TaxCodeRef")
                                    for line in lines):
            raise ValueError("QuickBooks export requires mapped line tax codes for invoice tax")

        freight = Decimal(str(payload.get("freight_amount") or "0"))
        if freight:
            if not self.freight_account_id:
                raise ValueError("QBO_FREIGHT_ACCOUNT_ID is required for invoices with freight")
            lines.append(self._account_line("Freight", self._decimal_number(freight),
                                            self.freight_account_id))
        discount = Decimal(str(payload.get("discount_amount") or "0"))
        if discount:
            if not self.discount_account_id:
                raise ValueError("QBO_DISCOUNT_ACCOUNT_ID is required for invoices with document discounts")
            lines.append(self._account_line("Invoice discount", -self._decimal_number(discount),
                                            self.discount_account_id))
        if not lines:
            raise ValueError("QuickBooks Bill export requires at least one line")

        bill = {
            "VendorRef": {"value": vendor_id},
            "DocNumber": invoice_number,
            "PrivateNote": f"{marker}; PO {payload.get('purchase_order_number', '')}",
            "CurrencyRef": {"value": str(payload.get("currency") or "USD").upper()},
            "Line": lines,
        }
        if self.ap_account_id:
            bill["APAccountRef"] = {"value": self.ap_account_id}
        params = {"requestid": idempotency_key}
        response = self._request("POST", "bill", params=params, body=bill)
        result = response.get("Bill") if isinstance(response, dict) else None
        if not isinstance(result, dict) or not result.get("Id"):
            raise RuntimeError("QuickBooks returned no Bill ID after create")
        self._validate_bill_total(result, payload)
        return str(result["Id"])

    @staticmethod
    def _validate_bill_total(bill: dict, payload: dict) -> None:
        if bill.get("TotalAmt") is None or payload.get("total_amount") is None:
            raise RuntimeError("QuickBooks Bill response did not include a total for reconciliation")
        actual = Decimal(str(bill["TotalAmt"]))
        expected = Decimal(str(payload["total_amount"]))
        if abs(actual - expected) > Decimal("0.01"):
            raise RuntimeError(
                f"QuickBooks Bill total {actual} differs from approved invoice total {expected}; "
                "check currency, tax-code, freight, and discount mappings"
            )

    @staticmethod
    def _account_line(description: str, amount: float, account_id: str) -> dict:
        return {
            "Description": description,
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": amount,
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_id}},
        }

    def _find_bill(self, invoice_number: str) -> dict | None:
        safe_number = invoice_number.replace("\\", "\\\\").replace("'", "\\'")
        query = f"select * from Bill where DocNumber = '{safe_number}'"
        response = self._request("GET", "query", params={"query": query})
        query_response = response.get("QueryResponse", {}) if isinstance(response, dict) else {}
        bills = query_response.get("Bill", []) if isinstance(query_response, dict) else []
        if isinstance(bills, dict):
            bills = [bills]
        return bills[0] if bills else None

    def _access_token(self, *, force_refresh: bool = False) -> str:
        with self.session_factory() as session:
            record = session.scalar(select(QuickBooksCredentialRecord).where(
                QuickBooksCredentialRecord.realm_id == self.realm_id
            ).with_for_update())
            now = datetime.now(timezone.utc)
            if record is not None and not force_refresh:
                expiry = (record.access_token_expires_at.replace(tzinfo=timezone.utc)
                          if record.access_token_expires_at.tzinfo is None
                          else record.access_token_expires_at)
                if expiry > now + timedelta(minutes=2):
                    return self._decrypt(record.encrypted_access_token)
            refresh_token = (
                self._decrypt(record.encrypted_refresh_token)
                if record is not None else self.bootstrap_refresh_token
            )
            if not refresh_token:
                raise ValueError(
                    "No stored QuickBooks token is available; set QBO_REFRESH_TOKEN after OAuth consent"
                )
            token_data = self._refresh(refresh_token)
            if record is None:
                record = QuickBooksCredentialRecord(
                    realm_id=self.realm_id,
                    encrypted_access_token=self._encrypt(token_data["access_token"]),
                    encrypted_refresh_token=self._encrypt(token_data["refresh_token"]),
                    access_token_expires_at=now + timedelta(seconds=token_data["expires_in"]),
                    updated_at=now,
                )
                session.add(record)
            else:
                record.encrypted_access_token = self._encrypt(token_data["access_token"])
                record.encrypted_refresh_token = self._encrypt(token_data["refresh_token"])
                record.access_token_expires_at = now + timedelta(seconds=token_data["expires_in"])
                record.updated_at = now
            session.commit()
            return token_data["access_token"]

    def _refresh(self, refresh_token: str) -> dict:
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            _TOKEN_URL,
            data=urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode(),
            headers={"Authorization": f"Basic {credentials}",
                     "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                data = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise QuickBooksAPIError(exc.code, detail) from exc
        except URLError as exc:
            raise RuntimeError(f"QuickBooks OAuth token refresh failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("QuickBooks OAuth endpoint returned invalid JSON") from exc
        if not data.get("access_token") or not (data.get("refresh_token") or refresh_token):
            raise RuntimeError("QuickBooks OAuth response omitted required token fields")
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh_token,
            "expires_in": int(data.get("expires_in", 3600)),
        }

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> dict:
        query = dict(params or {})
        if self.minor_version:
            query.setdefault("minorversion", self.minor_version)
        url = urljoin(self.base_url.rstrip("/") + "/",
                      f"v3/company/{self.realm_id}/{path.lstrip('/')}")
        if query:
            url = f"{url}?{urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token(force_refresh=attempt == 1)}",
                       "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                with self.opener(Request(url, data=data, headers=headers, method=method),
                                 timeout=self.timeout) as response:
                    raw = response.read(1_000_000)
            except HTTPError as exc:
                detail = exc.read(2000).decode("utf-8", errors="replace")
                if exc.code == 401 and attempt == 0:
                    continue
                raise QuickBooksAPIError(exc.code, detail) from exc
            except URLError as exc:
                raise RuntimeError(f"QuickBooks request failed: {exc.reason}") from exc
            if not raw:
                return {}
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError("QuickBooks API returned invalid JSON") from exc
            if isinstance(result, dict) and result.get("Fault"):
                raise RuntimeError(f"QuickBooks API fault: {json.dumps(result['Fault'])[:2000]}")
            if not isinstance(result, dict):
                raise RuntimeError("QuickBooks API returned an unexpected response")
            return result
        raise RuntimeError("QuickBooks authentication retry failed")

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("Could not decrypt QuickBooks credentials; verify QBO_TOKEN_ENCRYPTION_KEY") from exc
