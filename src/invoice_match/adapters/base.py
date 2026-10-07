from __future__ import annotations

from typing import Protocol


class PayablesDeliveryAdapter(Protocol):
    """AP boundary for idempotent payable delivery."""

    name: str

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str: ...


class APAdapter(PayablesDeliveryAdapter, Protocol):
    """Adapter that can also fetch source PO and receipt documents."""

    name: str

    def fetch_purchase_order(self, po_number: str) -> dict: ...

    def fetch_receipts(self, po_number: str) -> list[dict]: ...
