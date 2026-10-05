from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LineItem(StrictModel):
    sku: str = Field(min_length=1)
    qty: Decimal = Field(gt=0)
    unit: str = Field(default="EA", min_length=1, max_length=24)
    price: Decimal | None = Field(default=None, ge=0)
    discount_rate: Decimal = Field(default=Decimal(0), ge=0, le=1)
    tax_code: str | None = None
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    description: str | None = None


class PurchaseOrder(StrictModel):
    number: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    change_reason: str | None = None
    vendor: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_rate: Decimal | None = Field(default=None, ge=0, le=1)
    cost_center: str | None = None
    freight_amount: Decimal = Field(default=Decimal(0), ge=0)
    discount_amount: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def validate_revision(self):
        if self.revision > 1 and not self.change_reason:
            raise ValueError("change_reason is required for PO revisions after revision 1")
        return self


class ReceiptDocument(StrictModel):
    number: str | None = None
    items: list[LineItem] = Field(min_length=1)


class GoodsReceipt(StrictModel):
    number: str | None = None
    items: list[LineItem] = Field(default_factory=list)
    receipts: list[ReceiptDocument] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_receipt_lines(self):
        if not self.items and not self.receipts:
            raise ValueError("receipt requires items or one or more receipts")
        return self

    def all_items(self) -> list[LineItem]:
        return [*self.items, *(line for receipt in self.receipts for line in receipt.items)]


class SupplierInvoice(StrictModel):
    number: str | None = None
    vendor: str | None = None
    document_type: Literal["INVOICE", "CREDIT_NOTE"] = "INVOICE"
    credit_note_for: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    items: list[LineItem] = Field(min_length=1)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    freight_amount: Decimal = Field(default=Decimal(0), ge=0)
    discount_amount: Decimal = Field(default=Decimal(0), ge=0)
    total_amount: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_credit_note(self):
        if self.document_type == "CREDIT_NOTE" and not self.credit_note_for:
            raise ValueError("credit_note_for is required for a credit note")
        return self


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
    po_revision: int = 1
    invoice_number: str | None = None
    vendor: str
    base_currency: str
    invoice_total: Decimal = Decimal(0)
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    approval_status: Literal["NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"] = "PENDING"
    approval_policy: str = "default"
    required_role: str = "approver"
    escalation_role: str = "admin"
    approvals_required: int = 1
    approvals_received: int = 0
    assigned_to: str | None = None
    due_at: datetime | None = None


class ApprovalDecision(StrictModel):
    comment: str | None = None


class BulkApprovalDecision(ApprovalDecision):
    match_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_match_ids(self):
        if len(set(self.match_ids)) != len(self.match_ids):
            raise ValueError("match_ids must be unique")
        return self


class RuleVersionCreate(StrictModel):
    content: str = Field(min_length=1, max_length=100_000)


class RuleVersionResult(StrictModel):
    id: str
    version: int
    digest: str
    status: Literal["DRAFT", "ACTIVE", "RETIRED"]
    created_by: str
    created_at: str
    activated_by: str | None = None
    activated_at: str | None = None
    snapshot: dict


class RuleVersionAuditEntry(StrictModel):
    id: int
    rule_version_id: str
    event_type: str
    actor: str
    details: dict
    created_at: str


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
    escalated: bool = False
    escalated_to: str | None = None


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
    purchase_order_revision: int = 1
    supplier_invoice_number: str
    document_type: Literal["INVOICE", "CREDIT_NOTE"] = "INVOICE"
    credit_note_for: str | None = None
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
    attempt_count: int = 0
    last_error: str | None = None
    external_reference: str | None = None


class VerifyCapturedInvoice(StrictModel):
    invoice: SupplierInvoice


class CapturedMatchRequest(StrictModel):
    po: PurchaseOrder
    receipt: GoodsReceipt


class CapturedDocumentResult(StrictModel):
    document_id: str
    filename: str
    content_type: str
    source_sha256: str
    status: Literal["REVIEW_REQUIRED", "VERIFIED"]
    extracted_fields: dict
    confidence: dict[str, float]
    extraction_notes: list[str]
    reviewed_invoice: SupplierInvoice | None = None
    created_by: str
    created_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class DocumentCaptureAuditEntry(StrictModel):
    id: int
    document_id: str
    event_type: str
    actor: str
    details: dict
    created_at: datetime
