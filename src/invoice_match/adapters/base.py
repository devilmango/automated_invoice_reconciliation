from __future__ import annotations

from typing import Protocol


class APAdapter(Protocol):
    """Small AP boundary: fetch source documents and idempotently submit payables."""

    name: str

    def fetch_purchase_order(self, po_number: str) -> dict: ...

    def fetch_receipts(self, po_number: str) -> list[dict]: ...

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str: ...
