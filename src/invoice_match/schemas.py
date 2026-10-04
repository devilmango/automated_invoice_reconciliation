from __future__ import annotations

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
    description: str | None = None


class PurchaseOrder(StrictModel):
    number: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)


class GoodsReceipt(StrictModel):
    number: str | None = None
    items: list[LineItem] = Field(min_length=1)


class SupplierInvoice(StrictModel):
    number: str | None = None
    vendor: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_amount: Decimal | None = Field(default=None, ge=0)


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
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    approval_status: Literal["NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"] = "PENDING"


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


class AuditEntry(StrictModel):
    id: int
    match_id: str
    event_type: str
    actor: str
    details: dict
    created_at: str
