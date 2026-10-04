from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LineItem(StrictModel):
    sku: str = Field(min_length=1)
    qty: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, ge=0)
    discount_rate: Decimal = Field(default=Decimal(0), ge=0, le=1)
    tax_code: str | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    description: str | None = None


class PurchaseOrder(StrictModel):
    number: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    cost_center: str | None = None
    freight_amount: Decimal = Field(default=Decimal(0), ge=0)
    discount_amount: Decimal = Field(default=Decimal(0), ge=0)


class GoodsReceipt(StrictModel):
    number: str | None = None
    items: list[LineItem] = Field(min_length=1)


class SupplierInvoice(StrictModel):
    number: str | None = None
    vendor: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    freight_amount: Decimal = Field(default=Decimal(0), ge=0)
    discount_amount: Decimal = Field(default=Decimal(0), ge=0)
    total_amount: Decimal | None = Field(default=None, ge=0)


class MatchRequest(StrictModel):
    po: PurchaseOrder
    receipt: GoodsReceipt
    invoice: SupplierInvoice


class MatchStatus(StrEnum):
    MATCHED = "MATCHED"
    EXCEPTION = "EXCEPTION"


class Discrepancy(StrictModel):
    reason: str
    sku: str | None = None
    expected: Decimal | str | None = None
    invoiced: Decimal | str | None = None
    variance: Decimal | str | None = None
    message: str


class MatchResult(StrictModel):
    match_id: str | None = None
    status: MatchStatus
    reason: str | None = None
    expected: Decimal | str | None = None
    invoiced: Decimal | str | None = None
    variance: Decimal | str | None = None
    po_number: str
    invoice_number: str | None = None
    vendor: str
    base_currency: str
    invoice_total: Decimal = Decimal(0)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    approval_status: Literal["NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"] = "PENDING"
    approval_policy: str = "default"
    required_role: str = "approver"
    approvals_required: int = 1
    approvals_received: int = 0
    assigned_to: str | None = None
    due_at: datetime | None = None


class ApprovalDecision(StrictModel):
    comment: str | None = None


class ExceptionQueueItem(StrictModel):
    match_id: str
    status: MatchStatus
    approval_status: str
    po_number: str
    invoice_number: str | None
    vendor: str
    reason: str
    created_at: str
    approval_policy: str
    required_role: str
    approvals_received: int
    approvals_required: int
    assigned_to: str | None
    due_at: str | None


class AuditEntry(StrictModel):
    id: int
    match_id: str
    event_type: str
    actor: str
    details: dict
    created_at: str


class CsvMatchRequest(StrictModel):
    po_csv: str = Field(min_length=1)
    receipt_csv: str = Field(min_length=1)
    invoice_csv: str = Field(min_length=1)


class APExportLine(StrictModel):
    sku: str
    description: str | None = None
    quantity: Decimal
    unit_price: Decimal
    discount_rate: Decimal
    tax_code: str | None = None
    tax_rate: Decimal | None = None
    line_net: Decimal
    line_tax: Decimal


class APExportPayload(StrictModel):
    match_id: str
    purchase_order_number: str
    supplier_invoice_number: str
    supplier: str
    currency: str
    cost_center: str | None
    subtotal: Decimal
    freight_amount: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    lines: list[APExportLine]
    approval_status: str


class APOutboxItem(StrictModel):
    match_id: str
    state: Literal["PENDING", "ACKNOWLEDGED"]
    created_at: str
    acknowledged_at: str | None = None
    payload: APExportPayload
