from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class GenericHTTPAPAdapter:
    """Generic REST adapter contract, intended to be wrapped by vendor adapters."""

    name = "http"

    def __init__(self, endpoint: str, token: str, timeout: float = 15.0):
        self.endpoint = endpoint.rstrip("/") + "/"
        self.token = token
        self.timeout = timeout

    def fetch_purchase_order(self, po_number: str) -> dict:
        return self._request("GET", f"purchase-orders/{quote(po_number, safe='')}")

    def fetch_receipts(self, po_number: str) -> list[dict]:
        data = self._request("GET", f"purchase-orders/{quote(po_number, safe='')}/receipts")
        return data if isinstance(data, list) else data.get("receipts", [data])

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str:
        data = self._request("POST", "invoices", payload, idempotency_key=idempotency_key)
        if isinstance(data, dict):
            return str(data.get("id") or data.get("reference") or idempotency_key)
        return idempotency_key

    def _request(self, method: str, path: str, body: dict | None = None, *, idempotency_key: str | None = None):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(urljoin(self.endpoint, path), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_data = response.read()
        except HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            raise RuntimeError(f"AP adapter returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"AP adapter request failed: {exc.reason}") from exc
        if not response_data:
            return {}
        try:
            return json.loads(response_data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AP adapter returned a non-JSON response") from exc
