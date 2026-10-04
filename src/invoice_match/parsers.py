"""Small JSON document parsers; upstream OCR/ERP integrations can produce the same schema."""

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from .schemas import GoodsReceipt, PurchaseOrder, SupplierInvoice

T = TypeVar("T", bound=BaseModel)


def _parse(path: str | Path, schema: type[T]) -> T:
    return schema.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def parse_purchase_order(path: str | Path) -> PurchaseOrder:
    return _parse(path, PurchaseOrder)


def parse_goods_receipt(path: str | Path) -> GoodsReceipt:
    return _parse(path, GoodsReceipt)


def parse_supplier_invoice(path: str | Path) -> SupplierInvoice:
    return _parse(path, SupplierInvoice)
