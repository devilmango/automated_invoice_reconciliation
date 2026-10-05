from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO

from pydantic import ValidationError

from .schemas import MatchRequest


def _rows(source: str, document: str) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(StringIO(source))
        if not reader.fieldnames or any(not name for name in reader.fieldnames):
            raise ValueError(f"{document} CSV must include a header row")
        rows = [
            {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
        ]
    except csv.Error as exc:
        raise ValueError(f"Invalid {document} CSV: {exc}") from exc
    if not rows:
        raise ValueError(f"{document} CSV must include at least one data row")
    return rows


def _decimal(value: str, field: str) -> Decimal | None:
    if value == "":
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"CSV field {field!r} must be a decimal number") from exc


def _consistent(rows: list[dict[str, str]], fields: tuple[str, ...], document: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for field in fields:
        values = {row[field] for row in rows if row.get(field, "")}
        if len(values) > 1:
            raise ValueError(f"{document} CSV must use one {field} value per document")
        metadata[field] = next(iter(values), "")
    return metadata


def parse_csv_documents(po_csv: str, receipt_csv: str, invoice_csv: str) -> MatchRequest:
    po_rows, receipt_rows, invoice_rows = (
        _rows(po_csv, "PO"), _rows(receipt_csv, "receipt"), _rows(invoice_csv, "invoice")
    )
    po_meta = _consistent(
        po_rows,
        ("number", "vendor", "currency", "cost_center", "tax_rate", "freight_amount", "discount_amount", "revision", "change_reason"),
        "PO",
    )
    invoice_meta = _consistent(
        invoice_rows,
        ("number", "vendor", "currency", "tax_amount", "freight_amount", "discount_amount", "total_amount", "document_type", "credit_note_for"),
        "invoice",
    )
    def lines(rows: list[dict[str, str]], fields: tuple[str, ...]) -> list[dict]:
        result = []
        for row in rows:
            line = {field: row[field] for field in fields if row.get(field, "")}
            for field in ("qty", "price", "discount_rate", "tax_rate", "tax_amount"):
                if field in line:
                    line[field] = _decimal(line[field], field)
            result.append(line)
        return result

    po: dict = {
        "number": po_meta["number"], "vendor": po_meta["vendor"],
        "currency": po_meta["currency"] or "USD",
        "items": lines(po_rows, ("sku", "qty", "unit", "price", "discount_rate", "tax_code", "tax_rate", "tax_amount", "description")),
    }
    receipt_groups: dict[str, list[dict]] = {}
    for row in receipt_rows:
        receipt_groups.setdefault(row.get("number", ""), []).append(row)
    receipt = {"receipts": [
        {
            **({"number": number} if number else {}),
            "items": lines(group, ("sku", "qty", "unit", "description")),
        }
        for number, group in receipt_groups.items()
    ]}
    invoice: dict = {
        "currency": invoice_meta["currency"] or po_meta["currency"] or "USD",
        "items": lines(invoice_rows, ("sku", "qty", "unit", "price", "discount_rate", "tax_code", "tax_rate", "tax_amount", "description")),
    }
    if po_meta["cost_center"]:
        po["cost_center"] = po_meta["cost_center"]
    for field in ("tax_rate", "freight_amount", "discount_amount"):
        if po_meta[field]:
            po[field] = _decimal(po_meta[field], field)
    if po_meta["revision"]:
        try:
            po["revision"] = int(po_meta["revision"])
        except ValueError as exc:
            raise ValueError("CSV field 'revision' must be an integer") from exc
    if po_meta["change_reason"]:
        po["change_reason"] = po_meta["change_reason"]
    for field in ("number", "vendor"):
        if invoice_meta[field]:
            invoice[field] = invoice_meta[field]
    for field in ("document_type", "credit_note_for"):
        if invoice_meta[field]:
            invoice[field] = invoice_meta[field]
    for field in ("tax_amount", "freight_amount", "discount_amount", "total_amount"):
        if invoice_meta[field]:
            invoice[field] = _decimal(invoice_meta[field], field)
    try:
        return MatchRequest.model_validate({"po": po, "receipt": receipt, "invoice": invoice})
    except ValidationError as exc:
        raise ValueError(f"CSV documents do not match the required schema: {exc}") from exc
