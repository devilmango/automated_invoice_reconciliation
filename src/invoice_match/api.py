from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ap_export import build_ap_payload
from .adapters import create_adapter
from .csv_import import parse_csv_documents
from .database import (
    APOutboxRecord,
    AuditEvent,
    CapturedDocumentRecord,
    DocumentCaptureEvent,
    ExceptionRecord,
    MatchRecord,
    NotificationOutboxRecord,
    RuleVersionEvent,
    RuleVersionRecord,
    SessionLocal,
)
from .document_capture import extract_document
from .integration_auth import require_integration
from .matcher import match_documents
from .notification_delivery import escalate_overdue, queue_notification
from .delivery import deliver_match
from .vendor_profiles import canonical_vendor, load_vendor_profiles, normalize_name, vendor_aliases
from .reviewer_auth import Principal, require_admin, require_approver, require_reviewer
from .rules import ToleranceRules, load_rules, parse_rules_yaml
from .schemas import (
    APOutboxItem,
    APExportAcknowledgement,
    APReferenceReconciliation,
    ApprovalDecision,
    BulkApprovalDecision,
    AuditEntry,
    CapturedMatchRequest,
    CapturedDocumentResult,
    CsvMatchRequest,
    DocumentCaptureAuditEntry,
    ExceptionQueueItem,
    MatchRequest,
    MatchResult,
    MatchStatus,
    NotificationOutboxItem,
    RuleVersionCreate,
    RuleVersionAuditEntry,
    RuleVersionResult,
    ReviewerAPOutboxItem,
    SupplierInvoice,
    VerifyCapturedInvoice,
)

app = FastAPI(
    title="invoice-match",
    description="A lightweight, self-hosted three-way invoice matching engine for SMB finance teams.",
    version="0.3.0",
)


def get_session():
    with SessionLocal() as session:
        yield session


def _timestamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _safe_filename(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in ".-_ " else "_" for character in value)
    return safe.strip(" .")[:255] or "invoice-document"


def _normalized(value: str | None) -> str | None:
    return " ".join(value.casefold().split()) if value else None


def _invoice_amount(invoice: dict) -> Decimal | None:
    try:
        if invoice.get("total_amount") is not None:
            return Decimal(str(invoice["total_amount"]))
        subtotal = sum(
            Decimal(str(item["qty"])) * Decimal(str(item["price"]))
            for item in invoice.get("items", []) if item.get("price") is not None
        )
        if not invoice.get("items") or any(item.get("price") is None for item in invoice["items"]):
            return None
        return subtotal + Decimal(str(invoice.get("tax_amount") or 0)) \
            + Decimal(str(invoice.get("freight_amount") or 0)) \
            - Decimal(str(invoice.get("discount_amount") or 0))
    except (InvalidOperation, KeyError, TypeError):
        return None


def _invoice_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _check_duplicate_invoice(session: Session, request: MatchRequest, profiles: list[dict]) -> None:
    supplier = request.invoice.vendor or request.po.vendor
    canonical = canonical_vendor(supplier, profiles)
    vendor_keys = {
        _normalized(alias) for alias in vendor_aliases(supplier, profiles)
        if _normalized(alias)
    }
    if canonical:
        vendor_keys.add(_normalized(canonical))
    if not vendor_keys:
        return
    records = session.scalars(select(MatchRecord).where(MatchRecord.vendor_key.in_(vendor_keys))).all()
    incoming_number = normalize_name(request.invoice.number)
    incoming_amount = _invoice_amount(request.invoice.model_dump(mode="json"))
    incoming_date = request.invoice.invoice_date
    risks = []
    for record in records:
        old = record.input_data
        old_invoice = old.get("invoice", {})
        old_vendor = old_invoice.get("vendor") or old.get("po", {}).get("vendor")
        if normalize_name(canonical_vendor(old_vendor, profiles)) != normalize_name(canonical):
            continue
        if old_invoice.get("document_type", "INVOICE") != request.invoice.document_type:
            continue
        if (old_invoice.get("currency") or "USD").upper() != request.invoice.currency.upper():
            continue
        old_number = normalize_name(old_invoice.get("number"))
        if incoming_number and incoming_number == old_number:
            raise HTTPException(status_code=409, detail={
                "code": "DUPLICATE_SUPPLIER_INVOICE",
                "existing_match_id": record.id,
            })
        old_amount = _invoice_amount(old_invoice)
        amount_close = False
        if incoming_amount is not None and old_amount is not None:
            allowed_variance = max(Decimal("0.01"), max(abs(incoming_amount), abs(old_amount)) * Decimal("0.005"))
            amount_close = abs(incoming_amount - old_amount) <= allowed_variance
        old_date = _invoice_date(old_invoice.get("invoice_date"))
        dates_close = bool(
            incoming_date and old_date and abs((incoming_date - old_date).days) <= 3
        )
        if incoming_number and old_number:
            number_similarity = SequenceMatcher(None, incoming_number, old_number).ratio()
        else:
            number_similarity = 0
        date_conflict = bool(
            incoming_date and old_date and abs((incoming_date - old_date).days) > 7
        )
        number_close = number_similarity >= 0.92 and not date_conflict
        if amount_close and (number_close or dates_close):
            evidence = ["SAME_CANONICAL_VENDOR", "INVOICE_AMOUNT_WITHIN_0.5_PERCENT"]
            if number_close:
                evidence.append(f"INVOICE_NUMBER_SIMILARITY:{number_similarity:.2f}")
            if dates_close:
                evidence.append("INVOICE_DATE_WITHIN_3_DAYS")
            risks.append({
                "match_id": record.id,
                "invoice_number": old_invoice.get("number"),
                "invoice_date": old_invoice.get("invoice_date"),
                "invoice_amount": str(old_amount) if old_amount is not None else None,
                "number_similarity": round(number_similarity, 2),
                "evidence": evidence,
            })
    if risks:
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_INVOICE_RISK",
            "message": "A similar invoice already exists; review the candidate matches before resubmitting.",
            "candidates": risks[:5],
        })


def _request_hash(request: MatchRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result(record: MatchRecord) -> dict:
    data = dict(record.result_data)
    data.update({
        "approval_status": record.approval_status,
        "approvals_received": record.approvals_received,
        "approvals_required": record.approvals_required,
        "rule_version_id": record.rule_version_id,
        "rule_digest": record.rule_digest,
    })
    return data


def _add_outbox(session: Session, match_id: str, request: MatchRequest, rules) -> None:
    session.add(APOutboxRecord(
        match_id=match_id,
        payload=build_ap_payload(match_id, request, rules),
        state="PENDING",
    ))


def _active_rules(session: Session) -> tuple[ToleranceRules, RuleVersionRecord | None, str]:
    active = session.scalar(select(RuleVersionRecord).where(RuleVersionRecord.status == "ACTIVE"))
    if active is not None:
        return parse_rules_yaml(active.content), active, active.digest
    path = Path(os.getenv("INVOICE_MATCH_RULES", "config/rules.yaml"))
    content = path.read_text(encoding="utf-8") if path.exists() else "{}"
    return load_rules(), None, hashlib.sha256(content.encode("utf-8")).hexdigest()


def _rule_version_result(record: RuleVersionRecord) -> RuleVersionResult:
    return RuleVersionResult(
        id=record.id,
        version=record.version,
        digest=record.digest,
        status=record.status,
        created_by=record.created_by,
        created_at=_timestamp(record.created_at),
        activated_by=record.activated_by,
        activated_at=_timestamp(record.activated_at),
        snapshot=record.snapshot,
    )


def _submit_match(
    request: MatchRequest,
    *,
    session: Session,
    actor: str,
    idempotency_key: str,
    success_status: int = 201,
):
    request_digest = _request_hash(request)
    existing = session.scalar(select(MatchRecord).where(MatchRecord.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.request_hash != request_digest:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED"})
        return JSONResponse(status_code=200, content=_result(existing))

    profiles = load_vendor_profiles()
    vendor_key = _normalized(canonical_vendor(request.invoice.vendor or request.po.vendor, profiles)) or ""
    invoice_key = _normalized(request.invoice.number)
    _check_duplicate_invoice(session, request, profiles)

    po_vendor_keys = {
        _normalized(alias) for alias in vendor_aliases(request.po.vendor, profiles)
        if _normalized(alias)
    }
    po_vendor_keys.add(_normalized(canonical_vendor(request.po.vendor, profiles)))
    po_records = session.scalars(select(MatchRecord).where(
        MatchRecord.po_number == request.po.number,
        MatchRecord.vendor_key.in_(po_vendor_keys),
    )).all()
    latest_revision = max(
        (int(record.input_data.get("po", {}).get("revision", 1)) for record in po_records),
        default=0,
    )
    if request.po.revision < latest_revision:
        raise HTTPException(status_code=409, detail={
            "code": "STALE_PO_REVISION",
            "current_revision": latest_revision,
            "submitted_revision": request.po.revision,
        })
    previously_invoiced = [
        MatchRequest.model_validate(record.input_data).invoice
        for record in po_records
        if record.approval_status != "REJECTED"
    ]

    rules, rule_version, rule_digest = _active_rules(session)
    result = match_documents(request, rules, previously_invoiced=previously_invoiced)
    match_id = str(uuid4())
    result.match_id = match_id
    record = MatchRecord(
        id=match_id,
        status=result.status.value,
        approval_status=result.approval_status,
        po_number=request.po.number,
        invoice_number=request.invoice.number,
        vendor=request.po.vendor,
        vendor_key=vendor_key,
        invoice_number_key=invoice_key,
        idempotency_key=idempotency_key,
        request_hash=request_digest,
        cost_center=request.po.cost_center,
        reason=result.reason,
        approval_policy=result.approval_policy,
        required_role=result.required_role,
        escalation_role=next((
            policy.escalation_role for policy in rules.approval_policies
            if policy.name == result.approval_policy
        ), "admin"),
        approvals_required=result.approvals_required,
        approvals_received=0,
        assigned_to=result.assigned_to,
        due_at=result.due_at,
        rule_version_id=rule_version.id if rule_version else None,
        rule_digest=rule_digest,
        rule_snapshot=rules.model_dump(mode="json"),
        input_data=request.model_dump(mode="json"),
        result_data=result.model_dump(mode="json"),
    )
    session.add(record)
    if result.status == MatchStatus.EXCEPTION:
        exception = ExceptionRecord(
            match_id=match_id,
            queue_status="OPEN",
            discrepancies=[issue.model_dump(mode="json") for issue in result.discrepancies],
        )
        session.add(exception)
        queue_notification(session, record, "EXCEPTION_CREATED")
    elif result.approval_status in {"APPROVED", "NOT_REQUIRED"}:
        _add_outbox(session, match_id, request, rules)
    session.add(AuditEvent(
        match_id=match_id,
        event_type="MATCH_CREATED",
        actor=actor,
        details={
            "status": result.status.value,
            "reason": result.reason,
            "idempotency_key": idempotency_key,
            "po_revision": request.po.revision,
            "po_change_reason": request.po.change_reason,
            "previous_invoice_count": len(previously_invoiced),
            "rule_version_id": rule_version.id if rule_version else None,
            "rule_digest": rule_digest,
        },
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        idem_match = session.scalar(select(MatchRecord).where(MatchRecord.idempotency_key == idempotency_key))
        if idem_match is not None and idem_match.request_hash == request_digest:
            return JSONResponse(status_code=200, content=_result(idem_match))
        duplicate = session.scalar(select(MatchRecord).where(
            MatchRecord.vendor_key == vendor_key,
            MatchRecord.invoice_number_key == invoice_key,
        )) if invoice_key is not None else None
        if duplicate is not None:
            raise HTTPException(status_code=409, detail={
                "code": "DUPLICATE_SUPPLIER_INVOICE",
                "existing_match_id": duplicate.id,
            })
        raise HTTPException(status_code=409, detail={"code": "CONCURRENT_SUBMISSION_CONFLICT"})
    return JSONResponse(status_code=success_status, content=result.model_dump(mode="json"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/reviewer", include_in_schema=False)
def reviewer_dashboard():
    return FileResponse(Path(__file__).parent / "static" / "reviewer.html")


@app.get("/reviewer/captures", include_in_schema=False)
def reviewer_capture_inbox():
    return FileResponse(Path(__file__).parent / "static" / "reviewer-captures.html")


@app.get("/reviewer/operations", include_in_schema=False)
def reviewer_operations_dashboard():
    return FileResponse(Path(__file__).parent / "static" / "reviewer-operations.html")


def _captured_result(record: CapturedDocumentRecord) -> CapturedDocumentResult:
    return CapturedDocumentResult(
        document_id=record.id,
        filename=record.filename,
        content_type=record.content_type,
        source_sha256=record.source_sha256,
        status=record.status,
        extracted_fields=record.extracted_fields,
        confidence=record.confidence,
        extraction_notes=record.extraction_notes,
        reviewed_invoice=record.reviewed_invoice,
        created_by=record.created_by,
        created_at=record.created_at,
        reviewed_by=record.reviewed_by,
        reviewed_at=record.reviewed_at,
    )


@app.post("/documents/extract", response_model=CapturedDocumentResult, status_code=201)
async def capture_document(
    request: Request,
    session: Session = Depends(get_session),
    actor: str = Depends(require_integration),
    filename: str = Header(default="invoice-document", alias="X-Filename"),
):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if declared_size > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Documents are limited to 10 MiB")
    document = bytearray()
    async for chunk in request.stream():
        document.extend(chunk)
        if len(document) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Documents are limited to 10 MiB")
    if not document:
        raise HTTPException(status_code=400, detail="Document body cannot be empty")
    content_type = request.headers.get("content-type", "application/octet-stream")
    try:
        fields, confidence, notes, source_hash, normalized_type = extract_document(
            bytes(document), filename, content_type
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = session.scalar(select(CapturedDocumentRecord).where(
        CapturedDocumentRecord.source_sha256 == source_hash
    ))
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "code": "DUPLICATE_CAPTURED_DOCUMENT",
            "document_id": existing.id,
        })
    record = CapturedDocumentRecord(
        id=str(uuid4()),
        filename=_safe_filename(filename),
        content_type=normalized_type,
        source_sha256=source_hash,
        source_data=bytes(document),
        status="REVIEW_REQUIRED",
        extracted_fields=fields,
        confidence=confidence,
        extraction_notes=notes,
        created_by=actor,
    )
    session.add(record)
    session.flush()
    session.add(DocumentCaptureEvent(
        document_id=record.id,
        event_type="DOCUMENT_CAPTURED",
        actor=actor,
        details={"source_sha256": source_hash, "field_confidence": confidence},
    ))
    session.commit()
    return _captured_result(record)


@app.get("/documents", response_model=list[CapturedDocumentResult])
def list_captured_documents(
    status: str = Query(default="REVIEW_REQUIRED", pattern="^(REVIEW_REQUIRED|VERIFIED|ALL)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    query = select(CapturedDocumentRecord)
    if status != "ALL":
        query = query.where(CapturedDocumentRecord.status == status)
    records = session.scalars(query.order_by(CapturedDocumentRecord.created_at.asc()).limit(limit)).all()
    return [_captured_result(record) for record in records]


@app.get("/documents/{document_id}", response_model=CapturedDocumentResult)
def get_captured_document(
    document_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    record = session.get(CapturedDocumentRecord, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Captured document not found")
    return _captured_result(record)


@app.get("/documents/{document_id}/source")
def get_captured_source(
    document_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    record = session.get(CapturedDocumentRecord, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Captured document not found")
    return Response(
        content=record.source_data,
        media_type=record.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{record.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/documents/{document_id}/verify", response_model=CapturedDocumentResult)
def verify_captured_document(
    document_id: str,
    body: VerifyCapturedInvoice,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_approver),
):
    record = session.get(CapturedDocumentRecord, document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Captured document not found")
    if record.status != "REVIEW_REQUIRED":
        raise HTTPException(status_code=409, detail="Captured document has already been verified")
    if record.created_by == reviewer.name:
        raise HTTPException(status_code=403, detail="The document preparer cannot verify their own capture")
    record.status = "VERIFIED"
    record.reviewed_invoice = body.invoice.model_dump(mode="json")
    record.reviewed_by = reviewer.name
    record.reviewed_at = datetime.now(timezone.utc)
    session.add(DocumentCaptureEvent(
        document_id=document_id,
        event_type="DOCUMENT_VERIFIED",
        actor=reviewer.name,
        details={"invoice_number": body.invoice.number, "document_type": body.invoice.document_type},
    ))
    session.commit()
    return _captured_result(record)


@app.get("/documents/{document_id}/audit", response_model=list[DocumentCaptureAuditEntry])
def captured_document_audit(
    document_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    if session.get(CapturedDocumentRecord, document_id) is None:
        raise HTTPException(status_code=404, detail="Captured document not found")
    events = session.scalars(select(DocumentCaptureEvent)
        .where(DocumentCaptureEvent.document_id == document_id)
        .order_by(DocumentCaptureEvent.id.asc())).all()
    return [DocumentCaptureAuditEntry(
        id=event.id,
        document_id=event.document_id,
        event_type=event.event_type,
        actor=event.actor,
        details=event.details,
        created_at=event.created_at,
    ) for event in events]


@app.post("/matches", response_model=MatchResult, status_code=201)
def create_match(
    request: MatchRequest,
    session: Session = Depends(get_session),
    actor: str = Depends(require_integration),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
):
    return _submit_match(request, session=session, actor=actor, idempotency_key=idempotency_key)


@app.post("/imports/csv", response_model=MatchResult, status_code=201)
def import_csv(
    body: CsvMatchRequest,
    session: Session = Depends(get_session),
    actor: str = Depends(require_integration),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
):
    try:
        request = parse_csv_documents(body.po_csv, body.receipt_csv, body.invoice_csv)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _submit_match(request, session=session, actor=actor, idempotency_key=idempotency_key)


@app.post("/documents/{document_id}/match", response_model=MatchResult, status_code=201)
def match_verified_capture(
    document_id: str,
    body: CapturedMatchRequest,
    session: Session = Depends(get_session),
    actor: str = Depends(require_integration),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
):
    document = session.get(CapturedDocumentRecord, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Captured document not found")
    if document.status != "VERIFIED" or not document.reviewed_invoice:
        raise HTTPException(status_code=409, detail="Invoice capture must be verified before matching")
    request = MatchRequest(
        po=body.po,
        receipt=body.receipt,
        invoice=SupplierInvoice.model_validate(document.reviewed_invoice),
    )
    return _submit_match(request, session=session, actor=actor, idempotency_key=idempotency_key)


@app.get("/matches/{match_id}", response_model=MatchResult)
def get_match(
    match_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    record = session.get(MatchRecord, match_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return _result(record)


@app.get("/matches/{match_id}/documents")
def get_match_documents(
    match_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    record = session.get(MatchRecord, match_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return record.input_data


@app.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(
    status: str = Query(default="OPEN", pattern="^(OPEN|CLOSED|ALL)$"),
    q: str | None = Query(default=None, max_length=200),
    vendor: str | None = Query(default=None, max_length=255),
    po_number: str | None = Query(default=None, max_length=100),
    invoice_number: str | None = Query(default=None, max_length=100),
    reason: str | None = Query(default=None, max_length=100),
    assigned_to: str | None = Query(default=None, max_length=255),
    overdue_only: bool = False,
    escalated_only: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    query = select(ExceptionRecord, MatchRecord).join(MatchRecord, ExceptionRecord.match_id == MatchRecord.id)
    if status != "ALL":
        query = query.where(ExceptionRecord.queue_status == status)
    if q:
        needle = f"%{q.strip()}%"
        query = query.where(or_(
            MatchRecord.vendor.ilike(needle),
            MatchRecord.po_number.ilike(needle),
            MatchRecord.invoice_number.ilike(needle),
        ))
    if vendor:
        query = query.where(MatchRecord.vendor.ilike(f"%{vendor.strip()}%"))
    if po_number:
        query = query.where(MatchRecord.po_number.ilike(f"%{po_number.strip()}%"))
    if invoice_number:
        query = query.where(MatchRecord.invoice_number.ilike(f"%{invoice_number.strip()}%"))
    if reason:
        query = query.where(MatchRecord.reason.ilike(f"%{reason.strip()}%"))
    if assigned_to:
        query = query.where(MatchRecord.assigned_to.ilike(f"%{assigned_to.strip()}%"))
    if escalated_only:
        query = query.where(ExceptionRecord.escalated.is_(True))
    rows = session.execute(query.order_by(
        ExceptionRecord.escalated.desc(), ExceptionRecord.created_at.asc()
    )).all()
    if overdue_only:
        now = datetime.now(timezone.utc)
        rows = [
            (exception, match) for exception, match in rows
            if match.due_at is not None
            and (match.due_at.replace(tzinfo=timezone.utc) if match.due_at.tzinfo is None else match.due_at) < now
        ]
    rows = rows[offset:offset + limit]
    return [ExceptionQueueItem(
        match_id=match.id,
        status=MatchStatus(match.status),
        approval_status=match.approval_status,
        po_number=match.po_number,
        invoice_number=match.invoice_number,
        vendor=match.vendor,
        reason=match.reason or "UNKNOWN",
        created_at=_timestamp(exception.created_at),
        approval_policy=match.approval_policy,
        required_role=match.required_role,
        approvals_received=match.approvals_received,
        approvals_required=match.approvals_required,
        assigned_to=match.assigned_to,
        due_at=_timestamp(match.due_at),
        escalated=exception.escalated,
        escalated_to=exception.escalated_to,
    ) for exception, match in rows]


def _decide(
    match_id: str,
    decision: ApprovalDecision,
    outcome: str,
    principal: Principal,
    session: Session,
    *,
    commit: bool = True,
):
    record = session.scalar(
        select(MatchRecord).where(MatchRecord.id == match_id).with_for_update()
    )
    exception = session.scalar(select(ExceptionRecord).where(ExceptionRecord.match_id == match_id))
    if record is None or exception is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    if record.approval_status != "PENDING" or exception.queue_status != "OPEN":
        raise HTTPException(status_code=409, detail="This exception has already been decided")
    if record.required_role == "admin" and "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="This approval requires an admin role")
    if record.assigned_to and principal.name != record.assigned_to and "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="This exception is assigned to another reviewer")
    submitter = session.scalar(select(AuditEvent.actor).where(
        AuditEvent.match_id == match_id,
        AuditEvent.event_type == "MATCH_CREATED",
    ))
    if submitter == principal.name:
        raise HTTPException(status_code=403, detail="The match submitter cannot approve or reject their own exception")
    prior_vote = session.scalar(select(AuditEvent).where(
        AuditEvent.match_id == match_id,
        AuditEvent.event_type.in_(["EXCEPTION_APPROVAL_RECORDED", "EXCEPTION_APPROVED"]),
        AuditEvent.actor == principal.name,
    ))
    if prior_vote is not None:
        raise HTTPException(status_code=409, detail="A reviewer may only vote once on an exception")

    result = dict(record.result_data)
    if outcome == "REJECTED":
        record.approval_status = "REJECTED"
        exception.queue_status = "CLOSED"
        result["approval_status"] = "REJECTED"
        record.result_data = result
        session.add(AuditEvent(
            match_id=match_id,
            event_type="EXCEPTION_REJECTED",
            actor=principal.name,
            details={"comment": decision.comment, "approvals_received": record.approvals_received},
        ))
    else:
        record.approvals_received += 1
        result["approvals_received"] = record.approvals_received
        if record.approvals_received >= record.approvals_required:
            record.approval_status = "APPROVED"
            exception.queue_status = "CLOSED"
            result["approval_status"] = "APPROVED"
            record.result_data = result
            session.add(AuditEvent(
                match_id=match_id,
                event_type="EXCEPTION_APPROVED",
                actor=principal.name,
                details={
                    "comment": decision.comment,
                    "approvals_received": record.approvals_received,
                    "approvals_required": record.approvals_required,
                },
            ))
            request = MatchRequest.model_validate(record.input_data)
            _add_outbox(session, match_id, request, ToleranceRules.model_validate(record.rule_snapshot))
        else:
            record.result_data = result
            session.add(AuditEvent(
                match_id=match_id,
                event_type="EXCEPTION_APPROVAL_RECORDED",
                actor=principal.name,
                details={"comment": decision.comment, "approvals_received": record.approvals_received},
            ))
    if commit:
        session.commit()
    return _result(record)


@app.post("/exceptions/{match_id}/approve", response_model=MatchResult)
def approve_exception(
    match_id: str,
    decision: ApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_approver),
):
    return _decide(match_id, decision, "APPROVED", reviewer, session)


@app.post("/exceptions/{match_id}/reject", response_model=MatchResult)
def reject_exception(
    match_id: str,
    decision: ApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_approver),
):
    return _decide(match_id, decision, "REJECTED", reviewer, session)


@app.post("/exceptions/bulk-approve", response_model=list[MatchResult])
def bulk_approve_exceptions(
    body: BulkApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_approver),
):
    try:
        results = [
            _decide(match_id, body, "APPROVED", reviewer, session, commit=False)
            for match_id in body.match_ids
        ]
        session.commit()
        return results
    except Exception:
        session.rollback()
        raise


@app.post("/exceptions/bulk-reject", response_model=list[MatchResult])
def bulk_reject_exceptions(
    body: BulkApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_approver),
):
    try:
        results = [
            _decide(match_id, body, "REJECTED", reviewer, session, commit=False)
            for match_id in body.match_ids
        ]
        session.commit()
        return results
    except Exception:
        session.rollback()
        raise


@app.post("/exceptions/escalate-overdue")
def escalate_overdue_exceptions(
    session: Session = Depends(get_session),
    actor: Principal = Depends(require_approver),
):
    escalated = escalate_overdue(session, actor=actor.name)
    session.commit()
    return {"escalated_count": len(escalated), "match_ids": escalated}


@app.get("/notifications/outbox", response_model=list[NotificationOutboxItem])
def list_notification_outbox(
    state: str = Query(default="PENDING", pattern="^(PENDING|DELIVERED|DEAD|ALL)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    query = select(NotificationOutboxRecord)
    if state != "ALL":
        query = query.where(NotificationOutboxRecord.state == state)
    return session.scalars(query.order_by(NotificationOutboxRecord.created_at.asc()).limit(limit)).all()


@app.post("/notifications/outbox/{notification_id}/retry", response_model=NotificationOutboxItem)
def retry_notification(
    notification_id: str,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    record = session.get(NotificationOutboxRecord, notification_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if record.state != "DEAD":
        raise HTTPException(status_code=409, detail="Only dead-lettered notifications can be retried")
    record.state = "PENDING"
    record.next_attempt_at = None
    record.attempt_count = 0
    record.last_error = None
    session.commit()
    return record


@app.get("/rules/versions", response_model=list[RuleVersionResult])
def list_rule_versions(
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    versions = session.scalars(select(RuleVersionRecord).order_by(RuleVersionRecord.version.desc())).all()
    return [_rule_version_result(version) for version in versions]


@app.post("/rules/versions", response_model=RuleVersionResult, status_code=201)
def create_rule_version(
    body: RuleVersionCreate,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    try:
        rules = parse_rules_yaml(body.content)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid rules: {exc}") from exc
    digest = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    existing = session.scalar(select(RuleVersionRecord).where(RuleVersionRecord.digest == digest))
    if existing:
        raise HTTPException(status_code=409, detail={"code": "RULE_VERSION_ALREADY_EXISTS", "id": existing.id})
    latest = session.scalar(select(func.max(RuleVersionRecord.version))) or 0
    record = RuleVersionRecord(
        id=str(uuid4()), version=latest + 1, digest=digest, content=body.content,
        snapshot=rules.model_dump(mode="json"), status="DRAFT", created_by=admin.name,
    )
    session.add(record)
    session.flush()
    session.add(RuleVersionEvent(
        rule_version_id=record.id, event_type="RULE_VERSION_CREATED", actor=admin.name,
        details={"version": record.version, "digest": digest},
    ))
    session.commit()
    return _rule_version_result(record)


@app.post("/rules/versions/{version_id}/activate", response_model=RuleVersionResult)
def activate_rule_version(
    version_id: str,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    record = session.get(RuleVersionRecord, version_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Rule version not found")
    if record.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Only draft rule versions can be activated")
    if record.created_by == admin.name:
        raise HTTPException(status_code=403, detail="A different admin must activate this rule version")
    previous = session.scalars(select(RuleVersionRecord).where(RuleVersionRecord.status == "ACTIVE")).all()
    for version in previous:
        version.status = "RETIRED"
        session.add(RuleVersionEvent(
            rule_version_id=version.id,
            event_type="RULE_VERSION_RETIRED",
            actor=admin.name,
            details={"replaced_by": record.id, "version": record.version},
        ))
    session.flush()
    record.status = "ACTIVE"
    record.activated_by = admin.name
    record.activated_at = datetime.now(timezone.utc)
    session.add(RuleVersionEvent(
        rule_version_id=record.id, event_type="RULE_VERSION_ACTIVATED", actor=admin.name,
        details={"version": record.version, "digest": record.digest,
                 "retired_versions": [version.version for version in previous]},
    ))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Another rule version became active concurrently") from exc
    return _rule_version_result(record)


@app.get("/rules/versions/{version_id}/audit", response_model=list[RuleVersionAuditEntry])
def rule_version_audit(
    version_id: str,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    if session.get(RuleVersionRecord, version_id) is None:
        raise HTTPException(status_code=404, detail="Rule version not found")
    events = session.scalars(select(RuleVersionEvent).where(
        RuleVersionEvent.rule_version_id == version_id
    ).order_by(RuleVersionEvent.id.asc())).all()
    return [RuleVersionAuditEntry(
        id=event.id,
        rule_version_id=event.rule_version_id,
        event_type=event.event_type,
        actor=event.actor,
        details=event.details,
        created_at=_timestamp(event.created_at),
    ) for event in events]


@app.get("/integrations/ap/outbox", response_model=list[APOutboxItem])
def list_ap_outbox(
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    integration: str = Depends(require_integration),
):
    items = session.scalars(select(APOutboxRecord)
        .where(APOutboxRecord.state == "PENDING")
        .order_by(APOutboxRecord.created_at.asc())
        .limit(limit)).all()
    return [APOutboxItem(
        match_id=item.match_id,
        state=item.state,
        created_at=_timestamp(item.created_at),
        acknowledged_at=_timestamp(item.acknowledged_at),
        payload=item.payload,
        attempt_count=item.attempt_count,
        last_error=item.last_error,
        external_reference=item.external_reference,
    ) for item in items]


@app.post("/integrations/ap/outbox/{match_id}/ack", response_model=APOutboxItem)
def acknowledge_ap_outbox(
    match_id: str,
    body: APExportAcknowledgement | None = None,
    session: Session = Depends(get_session),
    integration: str = Depends(require_integration),
):
    item = session.get(APOutboxRecord, match_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    if item.state != "PENDING":
        raise HTTPException(status_code=409, detail="AP outbox item has already been acknowledged")
    item.state = "ACKNOWLEDGED"
    item.acknowledged_at = datetime.now(timezone.utc)
    if body and body.external_reference:
        item.external_reference = body.external_reference
    session.add(AuditEvent(
        match_id=match_id,
        event_type="AP_EXPORT_ACKNOWLEDGED",
        actor=integration,
        details={"external_reference": item.external_reference},
    ))
    session.commit()
    return APOutboxItem(
        match_id=item.match_id,
        state=item.state,
        created_at=_timestamp(item.created_at),
        acknowledged_at=_timestamp(item.acknowledged_at),
        payload=item.payload,
        attempt_count=item.attempt_count,
        last_error=item.last_error,
        external_reference=item.external_reference,
    )


@app.get("/reviewer/ap-outbox", response_model=list[ReviewerAPOutboxItem])
def reviewer_ap_outbox(
    state: str = Query(default="ALL", pattern="^(PENDING|ACKNOWLEDGED|ALL)$"),
    limit: int = Query(default=500, ge=1, le=1000),
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    query = select(APOutboxRecord, MatchRecord).join(
        MatchRecord, MatchRecord.id == APOutboxRecord.match_id
    )
    if state != "ALL":
        query = query.where(APOutboxRecord.state == state)
    rows = session.execute(query.order_by(APOutboxRecord.created_at.desc()).limit(limit)).all()
    return [ReviewerAPOutboxItem(
        match_id=item.match_id,
        state=item.state,
        vendor=match.vendor,
        invoice_number=match.invoice_number,
        currency=str(item.payload.get("currency") or "USD"),
        total_amount=item.payload.get("total_amount", 0),
        created_at=_timestamp(item.created_at),
        acknowledged_at=_timestamp(item.acknowledged_at),
        attempt_count=item.attempt_count,
        last_error=item.last_error,
        external_reference=item.external_reference,
    ) for item, match in rows]


@app.post("/reviewer/ap-outbox/{match_id}/retry", response_model=ReviewerAPOutboxItem)
def retry_ap_outbox(
    match_id: str,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    item = session.get(APOutboxRecord, match_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    if item.state != "PENDING":
        raise HTTPException(status_code=409, detail="Only pending AP exports can be retried")
    adapter_name = os.getenv("AP_DELIVERY_ADAPTER", "filesystem")
    try:
        adapter = create_adapter(adapter_name)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"AP delivery adapter is unavailable: {exc}") from exc
    session.add(AuditEvent(
        match_id=match_id,
        event_type="AP_EXPORT_RETRY_REQUESTED",
        actor=admin.name,
        details={"adapter": adapter_name},
    ))
    session.commit()
    try:
        result = deliver_match(session, adapter, match_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    match = session.get(MatchRecord, match_id)
    return ReviewerAPOutboxItem(
        match_id=result["match_id"],
        state=result["state"],
        vendor=match.vendor,
        invoice_number=match.invoice_number,
        currency=str(result["payload"].get("currency") or "USD"),
        total_amount=result["payload"].get("total_amount", 0),
        created_at=result["created_at"],
        acknowledged_at=result["acknowledged_at"],
        attempt_count=result["attempt_count"],
        last_error=result["last_error"],
        external_reference=result["external_reference"],
    )


@app.post("/reviewer/ap-outbox/{match_id}/reconcile", response_model=ReviewerAPOutboxItem)
def reconcile_ap_reference(
    match_id: str,
    body: APReferenceReconciliation,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    item = session.get(APOutboxRecord, match_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    if item.state != "ACKNOWLEDGED":
        raise HTTPException(status_code=409, detail="A delivery must be acknowledged before reconciliation")
    previous_reference = item.external_reference
    item.external_reference = body.external_reference
    session.add(AuditEvent(
        match_id=match_id,
        event_type="AP_EXPORT_REFERENCE_RECONCILED",
        actor=admin.name,
        details={
            "previous_reference": previous_reference,
            "external_reference": body.external_reference,
            "note": body.note,
        },
    ))
    session.commit()
    match = session.get(MatchRecord, match_id)
    return ReviewerAPOutboxItem(
        match_id=item.match_id,
        state=item.state,
        vendor=match.vendor,
        invoice_number=match.invoice_number,
        currency=str(item.payload.get("currency") or "USD"),
        total_amount=item.payload.get("total_amount", 0),
        created_at=_timestamp(item.created_at),
        acknowledged_at=_timestamp(item.acknowledged_at),
        attempt_count=item.attempt_count,
        last_error=item.last_error,
        external_reference=item.external_reference,
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@app.get("/reviewer/reports/operations")
def operations_report(
    start_date: date | None = None,
    end_date: date | None = None,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    today = datetime.now(timezone.utc).date()
    end_date = end_date or today
    start_date = start_date or end_date - timedelta(days=29)
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
    if (end_date - start_date).days > 365:
        raise HTTPException(status_code=422, detail="Report ranges cannot exceed 366 days")
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    match_filter = (MatchRecord.created_at >= start_at, MatchRecord.created_at < end_at)
    status_counts = dict(session.execute(
        select(MatchRecord.status, func.count(MatchRecord.id))
        .where(*match_filter).group_by(MatchRecord.status)
    ).all())
    total = sum(status_counts.values())
    matched = status_counts.get("MATCHED", 0)
    exceptions_total = status_counts.get("EXCEPTION", 0)
    reasons = session.execute(
        select(MatchRecord.reason, func.count(MatchRecord.id))
        .where(*match_filter, MatchRecord.status == "EXCEPTION")
        .group_by(MatchRecord.reason).order_by(func.count(MatchRecord.id).desc())
    ).all()
    vendor_rows = session.execute(
        select(MatchRecord.vendor, MatchRecord.status, MatchRecord.approval_status, func.count(MatchRecord.id))
        .where(*match_filter)
        .group_by(MatchRecord.vendor, MatchRecord.status, MatchRecord.approval_status)
    ).all()
    vendor_summary: dict[str, dict] = {}
    for vendor, status, approval_status, count in vendor_rows:
        entry = vendor_summary.setdefault(vendor, {
            "vendor": vendor, "matches": 0, "matched": 0, "exceptions": 0, "approved": 0,
        })
        entry["matches"] += count
        entry["matched"] += count if status == "MATCHED" else 0
        entry["exceptions"] += count if status == "EXCEPTION" else 0
        entry["approved"] += count if approval_status == "APPROVED" else 0
    final_decisions = session.execute(
        select(MatchRecord.created_at, AuditEvent.created_at)
        .join(AuditEvent, AuditEvent.match_id == MatchRecord.id)
            .where(*match_filter, AuditEvent.event_type.in_(["EXCEPTION_APPROVED", "EXCEPTION_REJECTED"]))
    ).all()
    turnaround_hours = [
        max(0.0, ((_utc(decided) - _utc(created)).total_seconds() / 3600))
        for created, decided in final_decisions
    ]
    open_rows = session.execute(
        select(ExceptionRecord, MatchRecord).join(
            MatchRecord, MatchRecord.id == ExceptionRecord.match_id
        ).where(*match_filter, ExceptionRecord.queue_status == "OPEN")
    ).all()
    now = datetime.now(timezone.utc)
    overdue = sum(1 for _, match in open_rows if _utc(match.due_at) and _utc(match.due_at) < now)
    escalated = sum(1 for exception, _ in open_rows if exception.escalated)
    outbox_rows = session.execute(
        select(APOutboxRecord).join(MatchRecord, MatchRecord.id == APOutboxRecord.match_id)
        .where(*match_filter)
    ).scalars().all()
    pending = [item for item in outbox_rows if item.state == "PENDING"]
    acknowledged = [item for item in outbox_rows if item.state == "ACKNOWLEDGED"]
    stalled_before = now - timedelta(hours=1)
    return {
        "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "matches": {
            "total": total,
            "matched": matched,
            "exceptions": exceptions_total,
            "match_rate_percent": round((matched / total * 100), 1) if total else 0.0,
        },
        "approvals": {
            "decisions": len(turnaround_hours),
            "average_turnaround_hours": round(mean(turnaround_hours), 2) if turnaround_hours else None,
            "median_turnaround_hours": round(median(turnaround_hours), 2) if turnaround_hours else None,
        },
        "exceptions": {
            "open": len(open_rows),
            "overdue": overdue,
            "escalated": escalated,
            "by_reason": [{"reason": reason or "UNKNOWN", "count": count} for reason, count in reasons],
        },
        "ap_exports": {
            "pending": len(pending),
            "stalled": sum(1 for item in pending if _utc(item.created_at) < stalled_before),
            "failed": sum(1 for item in pending if bool(item.last_error)),
            "acknowledged": len(acknowledged),
            "attempts": sum(item.attempt_count for item in outbox_rows),
        },
        "by_vendor": sorted(vendor_summary.values(), key=lambda item: (-item["matches"], item["vendor"].casefold())),
    }


@app.get("/matches/{match_id}/audit", response_model=list[AuditEntry])
def match_audit(
    match_id: str,
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    if session.get(MatchRecord, match_id) is None:
        raise HTTPException(status_code=404, detail="Match not found")
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.match_id == match_id).order_by(AuditEvent.id.asc())
    ).all()
    return [AuditEntry(
        id=event.id,
        match_id=event.match_id,
        event_type=event.event_type,
        actor=event.actor,
        details=event.details,
        created_at=_timestamp(event.created_at),
    ) for event in events]
