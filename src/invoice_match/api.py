from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ap_export import build_ap_payload
from .csv_import import parse_csv_documents
from .database import (
    APOutboxRecord,
    AuditEvent,
    CapturedDocumentRecord,
    DocumentCaptureEvent,
    ExceptionRecord,
    MatchRecord,
    SessionLocal,
)
from .document_capture import extract_document
from .integration_auth import require_integration
from .matcher import match_documents
from .reviewer_auth import Principal, require_approver, require_reviewer
from .rules import load_rules
from .schemas import (
    APOutboxItem,
    ApprovalDecision,
    AuditEntry,
    CapturedMatchRequest,
    CapturedDocumentResult,
    CsvMatchRequest,
    DocumentCaptureAuditEntry,
    ExceptionQueueItem,
    MatchRequest,
    MatchResult,
    MatchStatus,
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


def _request_hash(request: MatchRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result(record: MatchRecord) -> dict:
    data = dict(record.result_data)
    data.update({
        "approval_status": record.approval_status,
        "approvals_received": record.approvals_received,
        "approvals_required": record.approvals_required,
    })
    return data


def _add_outbox(session: Session, match_id: str, request: MatchRequest, rules) -> None:
    session.add(APOutboxRecord(
        match_id=match_id,
        payload=build_ap_payload(match_id, request, rules),
        state="PENDING",
    ))


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

    vendor_key = _normalized(request.invoice.vendor or request.po.vendor) or ""
    invoice_key = _normalized(request.invoice.number)
    if invoice_key is not None:
        duplicate = session.scalar(select(MatchRecord).where(
            MatchRecord.vendor_key == vendor_key,
            MatchRecord.invoice_number_key == invoice_key,
        ))
        if duplicate is not None:
            raise HTTPException(status_code=409, detail={
                "code": "DUPLICATE_SUPPLIER_INVOICE",
                "existing_match_id": duplicate.id,
            })

    po_records = session.scalars(select(MatchRecord).where(
        MatchRecord.po_number == request.po.number,
        MatchRecord.vendor_key == _normalized(request.po.vendor),
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

    rules = load_rules()
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
        approvals_required=result.approvals_required,
        approvals_received=0,
        assigned_to=result.assigned_to,
        due_at=result.due_at,
        input_data=request.model_dump(mode="json"),
        result_data=result.model_dump(mode="json"),
    )
    session.add(record)
    if result.status == MatchStatus.EXCEPTION:
        session.add(ExceptionRecord(
            match_id=match_id,
            queue_status="OPEN",
            discrepancies=[issue.model_dump(mode="json") for issue in result.discrepancies],
        ))
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


@app.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(
    status: str = Query(default="OPEN", pattern="^(OPEN|CLOSED|ALL)$"),
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    query = select(ExceptionRecord, MatchRecord).join(MatchRecord, ExceptionRecord.match_id == MatchRecord.id)
    if status != "ALL":
        query = query.where(ExceptionRecord.queue_status == status)
    rows = session.execute(query.order_by(ExceptionRecord.created_at.asc())).all()
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
    ) for exception, match in rows]


def _decide(match_id: str, decision: ApprovalDecision, outcome: str, principal: Principal, session: Session):
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
            _add_outbox(session, match_id, request, load_rules())
        else:
            record.result_data = result
            session.add(AuditEvent(
                match_id=match_id,
                event_type="EXCEPTION_APPROVAL_RECORDED",
                actor=principal.name,
                details={"comment": decision.comment, "approvals_received": record.approvals_received},
            ))
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
    session.add(AuditEvent(
        match_id=match_id,
        event_type="AP_EXPORT_ACKNOWLEDGED",
        actor=integration,
        details={},
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
