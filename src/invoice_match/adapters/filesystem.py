from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


class FilesystemAPAdapter:
    """Local reference adapter backed by JSON fixtures and idempotent JSON exports."""

    name = "filesystem"

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def list_purchase_orders(self, *, updated_since: datetime | None = None) -> list[dict]:
        order_dir = self.directory / "purchase_orders"
        if not order_dir.exists():
            return []
        orders = []
        for path in sorted(order_dir.glob("*.json")):
            if not path.is_file():
                continue
            order = self._read(path)
            wrapped = order.get("purchase_order", order)
            number = wrapped.get("number") if isinstance(wrapped, dict) else None
            modified_at = path.stat().st_mtime
            if number:
                receipt_path = self.directory / "receipts" / f"{quote(str(number), safe='')}.json"
                if receipt_path.exists():
                    modified_at = max(modified_at, receipt_path.stat().st_mtime)
            watermark = updated_since
            if watermark is not None:
                watermark = watermark.replace(tzinfo=timezone.utc) if watermark.tzinfo is None else watermark
            if watermark is not None and modified_at < watermark.timestamp():
                continue
            orders.append(order)
        return orders

    def fetch_purchase_order(self, po_number: str) -> dict:
        return self._read(self.directory / "purchase_orders" / f"{quote(po_number, safe='')}.json")

    def fetch_receipts(self, po_number: str) -> list[dict]:
        path = self.directory / "receipts" / f"{quote(po_number, safe='')}.json"
        if not path.exists():
            return []
        data = self._read(path)
        return data if isinstance(data, list) else data.get("receipts", [data])

    def submit_invoice(self, payload: dict, *, idempotency_key: str) -> str:
        export_dir = self.directory / "invoices"
        export_dir.mkdir(parents=True, exist_ok=True)
        target = export_dir / f"{quote(idempotency_key, safe='')}.json"
        if target.exists():
            existing = self._read(target)
            if existing != payload:
                raise ValueError("Idempotency key already exists for another AP payload")
            return target.stem
        fd, temp_name = tempfile.mkstemp(prefix=".invoice-match-", dir=export_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
            try:
                os.link(temp_name, target)
            except FileExistsError:
                existing = self._read(target)
                if existing != payload:
                    raise ValueError("Idempotency key already exists for another AP payload")
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target.stem

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"AP adapter input does not exist: {path}") from exc
