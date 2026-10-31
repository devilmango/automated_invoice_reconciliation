from __future__ import annotations

import hashlib
import json
import os
import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ap_export import build_ap_payload
from .adapters import create_adapter
from .adapters.factory import create_adapter_for_company
from .csv_import import parse_csv_documents
from .database import (
    AccountingConnectionEventRecord,
    AccountingConnectionRecord,
    AccountingConnectionHealthRecord,
    AccountingMappingChangeRecord,
    APOutboxRecord,
    PaymentAllocationRecord,
    AuditEvent,
    CapturedDocumentRecord,
    DocumentCaptureEvent,
    ExceptionRecord,
    MatchRecord,
    PaymentReconciliationEventRecord,
    RemittancePaymentRecord,
    NotificationOutboxRecord,
    ProcurementRecord,
    RuleVersionEvent,
    RuleVersionRecord,
    SessionLocal,
    WorkerHeartbeatRecord,
    AccountingMasterDataRecord,
    MappingSuggestionDecisionRecord,
    MasterDataSyncScheduleRecord,
    MasterDataSyncRunRecord,
    CreditNoteApplicationRecord,
    CreditApplicationOutboxRecord,
    BankStatementBatchRecord,
    BankAccountConfigurationRecord,
)
from .document_capture import extract_document
from .integration_auth import require_integration
from .matcher import match_documents
from .notification_delivery import escalate_overdue, queue_notification
from .master_data_sync import execute_master_data_sync, provider_failure_type
from .delivery import deliver_match
from .procurement_sync import sync_procurement
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
    AccountingConnectionUpsert,
    AccountingMappingUpdate,
    RemittancePaymentCreate,
    ManualPaymentAllocationRequest,
    BankStatementImport,
    CreditNoteApplication,
    Bai2TransactionCodeUpdate,
    BankReconciliationDecision,
    MappingSuggestionDecision,
    MasterDataSyncScheduleUpdate,
    AlertSnoozeRequest,
)
from .accounting_connections import (check_payload_mappings, decrypt_configuration,
                                     encrypt_configuration, mapping_field, public_connection,
                                     validate_configuration)
from .payment_reconciliation import _candidates as _payment_candidates
from .payment_reconciliation import manually_allocate, payment_result, reconcile_payment
from .accounting_status import refresh_accounting_status
from .bank_statement import parse_statement
from .credit_sync import retry_credit_application, sync_credit_applications as _sync_credit_applications

app = FastAPI(
    title="invoice-match",
    description="A lightweight, self-hosted three-way invoice matching engine for SMB finance teams.",
    version="0.3.0",
)


def get_session(request: Request):
    from .integration_auth import company_for_token
    from .oidc_auth import principal_from_cookie

    authorization = request.headers.get("authorization", "")
    token = authorization.split(None, 1)[1] if authorization.lower().startswith("bearer ") else None
    oidc_principal = principal_from_cookie(request) if not token else None
    with SessionLocal() as session:
        session.info["company_id"] = oidc_principal["company_id"] if oidc_principal else company_for_token(token)
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
def health(session: Session = Depends(get_session)):
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    return {"status": "ok", "database": "ok"}


@app.get("/reviewer/health/workers")
def worker_health(
    session: Session = Depends(get_session),
    reviewer: Principal = Depends(require_reviewer),
):
    now = datetime.now(timezone.utc)
    workers = session.scalars(select(WorkerHeartbeatRecord).order_by(
        WorkerHeartbeatRecord.worker_name.asc()
    )).all()
    results = []
    for worker in workers:
        succeeded = worker.last_succeeded_at
        if succeeded and succeeded.tzinfo is None:
            succeeded = succeeded.replace(tzinfo=timezone.utc)
        threshold = max(60, worker.poll_interval_seconds * 3)
        healthy = succeeded is not None and (now - succeeded).total_seconds() <= threshold
        results.append({
            "worker": worker.worker_name,
            "status": "DEGRADED" if worker.last_error else ("HEALTHY" if healthy else "STALE"),
            "last_started_at": _timestamp(worker.last_started_at),
            "last_succeeded_at": _timestamp(worker.last_succeeded_at),
            "last_error": worker.last_error,
            "poll_interval_seconds": worker.poll_interval_seconds,
        })
    return {"status": "ok" if all(row["status"] == "HEALTHY" for row in results) else "degraded",
            "workers": results}


@app.get("/reviewer", include_in_schema=False)
def reviewer_dashboard():
    return FileResponse(Path(__file__).parent / "static" / "reviewer.html")


@app.get("/auth/oidc/login", include_in_schema=False)
def oidc_login(return_to: str = "/reviewer"):
    from .oidc_auth import begin_login, cookie_options, state_cookie_name

    url, state_cookie = begin_login(return_to)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(state_cookie_name(), state_cookie, max_age=600, **cookie_options())
    return response


@app.get("/auth/oidc/callback", include_in_schema=False)
def oidc_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    from .oidc_auth import cookie_name, cookie_options, finish_login, session_ttl, state_cookie_name

    if error or not code or not state:
        raise HTTPException(status_code=401, detail="OIDC provider did not complete reviewer login")
    token, return_to = finish_login(code, state, request.cookies.get(state_cookie_name()))
    response = RedirectResponse(return_to + ("&" if "?" in return_to else "?") + "sso=1", status_code=303)
    response.set_cookie(cookie_name(), token, max_age=session_ttl(),
                        **cookie_options())
    response.delete_cookie(state_cookie_name(), path="/")
    return response


@app.post("/auth/oidc/logout", include_in_schema=False)
def oidc_logout():
    from .oidc_auth import cookie_name

    response = RedirectResponse("/reviewer", status_code=303)
    response.delete_cookie(cookie_name(), path="/")
    return response


@app.get("/auth/oidc/me", include_in_schema=False)
def oidc_identity(principal: Principal = Depends(require_reviewer)):
    return {"name": principal.name, "roles": sorted(principal.roles), "company_id": principal.company_id}


@app.get("/reviewer/captures", include_in_schema=False)
def reviewer_capture_inbox():
    return FileResponse(Path(__file__).parent / "static" / "reviewer-captures.html")


@app.get("/reviewer/operations", include_in_schema=False)
def reviewer_operations_dashboard():
    return FileResponse(Path(__file__).parent / "static" / "reviewer-operations.html")


@app.get("/reviewer/bank-reconciliation", include_in_schema=False)
def reviewer_bank_reconciliation_dashboard():
    return FileResponse(Path(__file__).parent / "static" / "reviewer-bank-reconciliation.html")


@app.post("/admin/procurement/sync")
def synchronize_procurement(
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    adapter_name = os.getenv("AP_PROCUREMENT_ADAPTER", "filesystem")
    try:
        adapter = create_adapter(adapter_name, company_id=session.info.get("company_id", "default"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Procurement adapter is unavailable: {exc}") from exc
    if not callable(getattr(adapter, "list_purchase_orders", None)) or not callable(
        getattr(adapter, "fetch_receipts", None)
    ):
        raise HTTPException(status_code=503, detail=f"The {adapter_name!r} adapter cannot sync procurement documents")
    return sync_procurement(session, adapter)


@app.get("/integrations/procurement/purchase-orders/{po_number}")
def get_synced_procurement(
    po_number: str,
    session: Session = Depends(get_session),
    integration: str = Depends(require_integration),
):
    record = session.get(ProcurementRecord, (session.info.get("company_id", "default"), po_number))
    if record is None:
        raise HTTPException(status_code=404, detail="Purchase order has not been synchronized")
    return {
        "purchase_order": record.purchase_order,
        "receipts": record.receipts,
        "source_adapter": record.source_adapter,
        "synced_at": _timestamp(record.synced_at),
    }


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


@app.get("/admin/alerts")
def list_admin_alerts(
    state: str = Query(default="ALL", pattern="^(ALL|OPEN|ACKNOWLEDGED|SNOOZED)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    alerts = session.scalars(select(NotificationOutboxRecord).where(
        NotificationOutboxRecord.provider.is_not(None),
        NotificationOutboxRecord.event_type.in_(
            ("MASTER_DATA_SYNC_FAILED", "MASTER_DATA_SYNC_STALE", "MASTER_DATA_SYNC_RECOVERED")),
    ).order_by(NotificationOutboxRecord.created_at.desc()).limit(limit)).all()
    providers = {alert.provider for alert in alerts}
    history_events = session.scalars(select(AccountingConnectionEventRecord).where(
        AccountingConnectionEventRecord.provider.in_(providers or {"__none__"}),
        AccountingConnectionEventRecord.event_type.in_(
            ("ADMIN_ALERT_DELIVERED", "ADMIN_ALERT_DELIVERY_FAILED",
             "ADMIN_ALERT_ACKNOWLEDGED", "ADMIN_ALERT_SNOOZED")),
    ).order_by(AccountingConnectionEventRecord.created_at.asc())).all()
    event_history: dict[str, list[dict]] = {}
    for event in history_events:
        notification_id = event.details.get("notification_id")
        if notification_id:
            event_history.setdefault(notification_id, []).append({
                "event_type": event.event_type, "actor": event.actor,
                "details": event.details, "created_at": _timestamp(event.created_at)})
    results = []
    for alert in alerts:
        snoozed_until = _utc(alert.snoozed_until)
        if alert.acknowledged_at:
            status = "ACKNOWLEDGED"
        elif snoozed_until and snoozed_until > now:
            status = "SNOOZED"
        else:
            status = "OPEN"
        if state != "ALL" and status != state:
            continue
        results.append({"id": alert.id, "provider": alert.provider,
            "event_type": alert.event_type, "delivery_state": alert.state,
            "management_state": status, "payload": alert.payload,
            "attempt_count": alert.attempt_count, "next_attempt_at": _timestamp(alert.next_attempt_at),
            "last_error": alert.last_error, "provider_reference": alert.provider_reference,
            "created_at": _timestamp(alert.created_at), "delivered_at": _timestamp(alert.delivered_at),
            "acknowledged_at": _timestamp(alert.acknowledged_at),
            "acknowledged_by": alert.acknowledged_by,
            "snoozed_until": _timestamp(alert.snoozed_until), "snoozed_by": alert.snoozed_by,
            "history": event_history.get(alert.id, [])})
    return results


@app.post("/admin/alerts/{alert_id}/acknowledge")
def acknowledge_admin_alert(
    alert_id: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    alert = session.get(NotificationOutboxRecord, alert_id)
    if alert is None or alert.provider is None:
        raise HTTPException(status_code=404, detail="Provider alert not found")
    if alert.acknowledged_at is not None:
        return {"id": alert.id, "management_state": "ACKNOWLEDGED", "idempotent": True}
    now = datetime.now(timezone.utc)
    alert.acknowledged_at, alert.acknowledged_by = now, admin.name
    alert.snoozed_until, alert.snoozed_by = None, None
    session.add(AccountingConnectionEventRecord(provider=alert.provider,
        event_type="ADMIN_ALERT_ACKNOWLEDGED", actor=admin.name,
        details={"notification_id": alert.id, "event_type": alert.event_type,
                 "delivery_state": alert.state}))
    session.commit()
    return {"id": alert.id, "management_state": "ACKNOWLEDGED", "idempotent": False}


@app.post("/admin/alerts/{alert_id}/snooze")
def snooze_admin_alert(
    alert_id: str, body: AlertSnoozeRequest, session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    alert = session.get(NotificationOutboxRecord, alert_id)
    if alert is None or alert.provider is None:
        raise HTTPException(status_code=404, detail="Provider alert not found")
    if alert.acknowledged_at is not None:
        raise HTTPException(status_code=409, detail="An acknowledged alert cannot be snoozed")
    snoozed_until = body.snoozed_until
    if snoozed_until.tzinfo is None:
        snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
    alert.snoozed_until, alert.snoozed_by = snoozed_until, admin.name
    session.add(AccountingConnectionEventRecord(provider=alert.provider,
        event_type="ADMIN_ALERT_SNOOZED", actor=admin.name,
        details={"notification_id": alert.id, "event_type": alert.event_type,
                 "snoozed_until": _timestamp(snoozed_until)}))
    session.commit()
    return {"id": alert.id, "management_state": "SNOOZED",
            "snoozed_until": _timestamp(snoozed_until)}


@app.get("/admin/accounting-connections/audit-export")
def export_accounting_audit(
    start_date: date = Query(), end_date: date = Query(),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    """Download a date-bounded CSV of sync, mapping, connection, and alert audit data."""
    if end_date < start_date or (end_date - start_date).days > 366:
        raise HTTPException(status_code=422, detail="Choose an ordered date range no longer than 367 days")
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_before = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    rows: list[dict] = []

    def add(record_type: str, event_id: str, occurred_at: datetime | None, provider: str | None,
            actor: str | None, event_type: str, status: str | None, details: dict | None) -> None:
        rows.append({"company_id": session.info.get("company_id", "default"),
            "record_type": record_type, "event_id": event_id,
            "occurred_at": _timestamp(occurred_at), "provider": provider or "",
            "actor": actor or "", "event_type": event_type, "status": status or "",
            "details": json.dumps(details or {}, sort_keys=True, default=str, separators=(",", ":"))})

    runs = session.scalars(select(MasterDataSyncRunRecord).where(
        MasterDataSyncRunRecord.started_at >= start_at,
        MasterDataSyncRunRecord.started_at < end_before).order_by(MasterDataSyncRunRecord.started_at)).all()
    for run in runs:
        add("SYNC_RUN", run.id, run.started_at, run.provider, run.actor, run.trigger_type,
            run.status, {"finished_at": run.finished_at, "duration_ms": run.duration_ms,
                         "counts": run.counts, "changes": run.changes, "details": run.details,
                         "error": run.error})

    mappings = session.scalars(select(AccountingMappingChangeRecord).where(
        AccountingMappingChangeRecord.created_at >= start_at,
        AccountingMappingChangeRecord.created_at < end_before
    ).order_by(AccountingMappingChangeRecord.created_at)).all()
    for change in mappings:
        add("MAPPING_REVISION", change.id, change.created_at, change.provider, change.actor,
            change.change_type, None, {"category": change.category, "source_label": change.source_label,
                "previous_source_id": change.previous_source_id,
                "previous_source_label": change.previous_source_label,
                "new_source_id": change.new_source_id, "new_source_label": change.new_source_label,
                "reverts_change_id": change.reverts_change_id})

    connection_events = session.scalars(select(AccountingConnectionEventRecord).where(
        AccountingConnectionEventRecord.created_at >= start_at,
        AccountingConnectionEventRecord.created_at < end_before,
    ).order_by(AccountingConnectionEventRecord.created_at)).all()
    for event in connection_events:
        if event.event_type in {"ACCOUNTING_CONNECTION_TESTED", "ACCOUNTING_CONNECTION_HEALTH_CHECKED",
                                "ACCOUNTING_CONNECTION_HEALTH_CHECK_FAILED"}:
            add("CONNECTION_CHECK", str(event.id), event.created_at, event.provider, event.actor,
                event.event_type, event.details.get("status"), event.details)
        elif event.event_type.startswith("ADMIN_ALERT_") or event.event_type == "ACCOUNTING_MASTER_DATA_SYNC_RECOVERED":
            add("ALERT_EVENT", str(event.id), event.created_at, event.provider, event.actor,
                event.event_type, None, event.details)

    alerts = session.scalars(select(NotificationOutboxRecord).where(
        NotificationOutboxRecord.provider.is_not(None),
        NotificationOutboxRecord.created_at >= start_at,
        NotificationOutboxRecord.created_at < end_before,
    ).order_by(NotificationOutboxRecord.created_at)).all()
    for alert in alerts:
        add("ALERT", alert.id, alert.created_at, alert.provider, None, alert.event_type,
            alert.state, {"payload": alert.payload, "attempt_count": alert.attempt_count,
                "next_attempt_at": alert.next_attempt_at, "last_error": alert.last_error,
                "provider_reference": alert.provider_reference, "delivered_at": alert.delivered_at,
                "acknowledged_at": alert.acknowledged_at, "acknowledged_by": alert.acknowledged_by,
                "snoozed_until": alert.snoozed_until, "snoozed_by": alert.snoozed_by})

    rows.sort(key=lambda item: item["occurred_at"] or "")
    output = io.StringIO(newline="")
    fieldnames = ("company_id", "record_type", "event_id", "occurred_at", "provider", "actor",
                  "event_type", "status", "details")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        for key, value in row.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                row[key] = "'" + value
        writer.writerow(row)
    filename = f"invoice-match-audit-{start_date.isoformat()}-to-{end_date.isoformat()}.csv"
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
        .where(
            APOutboxRecord.state == "PENDING",
            (APOutboxRecord.next_attempt_at.is_(None)
             | (APOutboxRecord.next_attempt_at <= datetime.now(timezone.utc))),
        )
        .order_by(APOutboxRecord.created_at.asc())
        .limit(limit)).all()
    return [APOutboxItem(
        match_id=item.match_id,
        state=item.state,
        created_at=_timestamp(item.created_at),
        acknowledged_at=_timestamp(item.acknowledged_at),
        payload=item.payload,
        attempt_count=item.attempt_count,
        next_attempt_at=_timestamp(item.next_attempt_at),
        last_error=item.last_error,
        external_reference=item.external_reference,
        accounting_status=item.accounting_status,
        accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
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
    item.next_attempt_at = None
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
        next_attempt_at=_timestamp(item.next_attempt_at),
        last_error=item.last_error,
        external_reference=item.external_reference,
        accounting_status=item.accounting_status,
        accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
    )


@app.get("/reviewer/ap-outbox", response_model=list[ReviewerAPOutboxItem])
def reviewer_ap_outbox(
    state: str = Query(default="ALL", pattern="^(PENDING|ACKNOWLEDGED|DEAD|ALL)$"),
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
    try:
        adapter = create_adapter_for_company(session.info.get("company_id", "default"))
    except (ValueError, RuntimeError):
        adapter = None
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
        next_attempt_at=_timestamp(item.next_attempt_at),
        last_error=item.last_error,
        external_reference=item.external_reference,
        accounting_status=item.accounting_status,
        accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
        accounting_status_attempt_count=item.accounting_status_attempt_count,
        accounting_status_next_retry_at=_timestamp(item.accounting_status_next_retry_at),
        accounting_status_error=item.accounting_status_error,
        mapping_issues=check_payload_mappings(adapter, item.payload) if adapter else [],
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
    if item.state not in {"PENDING", "DEAD"}:
        raise HTTPException(status_code=409, detail="Only pending or dead-lettered AP exports can be retried")
    adapter_name = os.getenv("AP_DELIVERY_ADAPTER", "filesystem")
    try:
        adapter = create_adapter_for_company(session.info.get("company_id", "default"))
        adapter_name = adapter.name
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"AP delivery adapter is unavailable: {exc}") from exc
    mapping_issues = check_payload_mappings(adapter, item.payload)
    if mapping_issues:
        session.add(AuditEvent(match_id=match_id, event_type="AP_EXPORT_BLOCKED_MISSING_MAPPING",
            actor=admin.name, details={"issues": mapping_issues}))
        session.commit()
        raise HTTPException(status_code=409, detail={
            "code": "ACCOUNTING_MAPPINGS_MISSING",
            "message": "Complete the listed accounting mappings before retrying this export.",
            "issues": mapping_issues,
        })
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
        accounting_status=item.accounting_status,
        accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
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
        next_attempt_at=_timestamp(item.next_attempt_at),
        last_error=item.last_error,
        external_reference=item.external_reference,
        accounting_status=item.accounting_status,
        accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@app.get("/admin/accounting-connections")
def list_accounting_connections(
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    rows = session.scalars(select(AccountingConnectionRecord).order_by(AccountingConnectionRecord.provider)).all()
    return [public_connection(row) for row in rows]


@app.put("/admin/accounting-connections/{provider}")
def upsert_accounting_connection(
    provider: str, body: AccountingConnectionUpsert,
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    try:
        configuration = validate_configuration(provider, body.configuration)
        encrypted = encrypt_configuration(configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider
    ))
    now = datetime.now(timezone.utc)
    if record is None:
        record = AccountingConnectionRecord(id=str(uuid4()), provider=provider,
                                            encrypted_config=encrypted, enabled=body.enabled,
                                            updated_by=admin.name)
        session.add(record)
        event_type = "ACCOUNTING_CONNECTION_CREATED"
    else:
        record.encrypted_config = encrypted
        record.enabled = body.enabled
        record.updated_by = admin.name
        record.updated_at = now
        event_type = "ACCOUNTING_CONNECTION_UPDATED"
    session.add(AccountingConnectionEventRecord(provider=provider, event_type=event_type,
                                                actor=admin.name,
                                                details={"enabled": body.enabled}))
    session.commit()
    session.refresh(record)
    return public_connection(record)


@app.delete("/admin/accounting-connections/{provider}")
def disable_accounting_connection(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    record = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    record.enabled = False
    record.updated_by = admin.name
    record.updated_at = datetime.now(timezone.utc)
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_CONNECTION_DISABLED", actor=admin.name, details={}))
    session.commit()
    return {"provider": provider, "enabled": False}


@app.get("/admin/accounting-connections/events")
def accounting_connection_events(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    events = session.scalars(select(AccountingConnectionEventRecord).order_by(
        AccountingConnectionEventRecord.created_at.desc()).limit(limit)).all()
    return [{"id": event.id, "provider": event.provider, "event_type": event.event_type,
             "actor": event.actor, "details": event.details, "created_at": _timestamp(event.created_at)}
            for event in events]


@app.post("/admin/accounting-connections/{provider}/sync-masters")
def sync_accounting_masters(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider, AccountingConnectionRecord.enabled.is_(True)
    ))
    if connection is None:
        raise HTTPException(status_code=404, detail="Enabled accounting connection not found")
    try:
        run = execute_master_data_sync(session, provider=provider,
            company_id=session.info.get("company_id", "default"),
            adapter_factory=lambda name, company: create_adapter(name, company_id=company),
            apply_sync=_apply_master_data_sync, actor=admin.name, trigger_type="MANUAL")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Accounting master-data sync failed: {exc}") from exc
    return {"provider": provider, "synced_at": _timestamp(run.finished_at),
            "counts": run.counts, "changes": run.changes, "details": run.details,
            "run": master_data_sync_run_result(run)}


@app.post("/admin/accounting-connections/{provider}/sync-preview")
def preview_accounting_master_sync(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    """Fetch provider master data and show its proposed diff without persisting changes."""
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider, AccountingConnectionRecord.enabled.is_(True)))
    if connection is None:
        raise HTTPException(status_code=404, detail="Enabled accounting connection not found")
    try:
        adapter = create_adapter(provider, company_id=session.info.get("company_id", "default"))
        if adapter.name != provider or not callable(getattr(adapter, "fetch_master_data", None)):
            raise ValueError(f"The {provider} adapter does not support master-data sync")
        references = adapter.fetch_master_data()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Accounting master-data preview failed: {exc}") from exc
    counts, changes, details = _master_data_diff(session, provider, references)
    return {"provider": provider, "previewed_at": _timestamp(datetime.now(timezone.utc)),
            "applies_changes": False, "counts": counts, "changes": changes, "details": details}


def _apply_master_data_sync(session: Session, provider: str, adapter, actor: str) -> tuple[dict, dict]:
    if adapter.name != provider or not callable(getattr(adapter, "fetch_master_data", None)):
        raise ValueError(f"The {provider} adapter does not support master-data sync")
    references = adapter.fetch_master_data()
    counts, changes, details = _master_data_diff(session, provider, references)
    now = datetime.now(timezone.utc)
    for category, items in references.items():
        if category not in {"supplier", "item", "tax", "currency"}:
            continue
        current = session.scalars(select(AccountingMasterDataRecord).where(
            AccountingMasterDataRecord.provider == provider,
            AccountingMasterDataRecord.category == category,
        )).all()
        by_source = {row.source_id: row for row in current}
        incoming_ids = {str(item["source_id"]) for item in items}
        for row in current:
            if row.active and row.source_id not in incoming_ids:
                row.active = False
        for item in items:
            source_id = str(item["source_id"])
            record = by_source.get(source_id)
            if record is None:
                record = AccountingMasterDataRecord(id=str(uuid4()), provider=provider,
                    category=category, source_id=source_id, name=str(item["name"])[:255],
                    code=item.get("code"), active=bool(item.get("active", True)),
                    source_data=item.get("source_data") or {}, synced_at=now)
                session.add(record)
            else:
                new_name = str(item["name"])[:255]
                new_source_data = item.get("source_data") or {}
                new_active = bool(item.get("active", True))
                record.name, record.code = new_name, item.get("code")
                record.active, record.source_data, record.synced_at = new_active, new_source_data, now
    session.add(AccountingConnectionEventRecord(provider=provider, event_type="ACCOUNTING_MASTER_DATA_SYNCED",
        actor=actor, details={"counts": counts, "changes": changes, "details": details}))
    return counts, changes, details


def _master_data_diff(session: Session, provider: str, references: dict) -> tuple[dict, dict, dict]:
    """Calculate a side-effect-free record-level diff against the last provider snapshot."""
    counts, changes, details = {}, {}, {}
    for category, items in references.items():
        if category not in {"supplier", "item", "tax", "currency"}:
            continue
        current = session.scalars(select(AccountingMasterDataRecord).where(
            AccountingMasterDataRecord.provider == provider,
            AccountingMasterDataRecord.category == category)).all()
        by_source = {row.source_id: row for row in current}
        incoming_ids = {str(item["source_id"]) for item in items}
        category_changes = {"added": 0, "changed": 0, "deactivated": 0}
        category_details = []
        for row in current:
            if row.active and row.source_id not in incoming_ids:
                category_changes["deactivated"] += 1
                category_details.append({"action": "DEACTIVATED", "source_id": row.source_id,
                    "before": {"name": row.name, "code": row.code, "active": True},
                    "after": {"name": row.name, "code": row.code, "active": False}})
        for item in items:
            source_id = str(item["source_id"])
            name = str(item["name"])[:255]
            code = item.get("code")
            active = bool(item.get("active", True))
            record = by_source.get(source_id)
            if record is None:
                category_changes["added"] += 1
                category_details.append({"action": "ADDED", "source_id": source_id,
                    "before": None, "after": {"name": name, "code": code, "active": active}})
            elif record.active and not active:
                category_changes["deactivated"] += 1
                category_details.append({"action": "DEACTIVATED", "source_id": source_id,
                    "before": {"name": record.name, "code": record.code, "active": record.active},
                    "after": {"name": name, "code": code, "active": active}})
            elif (record.name != name or record.code != code or record.active != active or
                  record.source_data != (item.get("source_data") or {})):
                category_changes["changed"] += 1
                category_details.append({"action": "CHANGED", "source_id": source_id,
                    "before": {"name": record.name, "code": record.code, "active": record.active},
                    "after": {"name": name, "code": code, "active": active}})
        counts[category] = len(items)
        changes[category] = category_changes
        details[category] = category_details
    return counts, changes, details


def master_data_sync_run_result(run: MasterDataSyncRunRecord) -> dict:
    return {"id": run.id, "provider": run.provider, "trigger_type": run.trigger_type,
            "actor": run.actor, "status": run.status, "counts": run.counts,
            "changes": run.changes, "details": run.details, "duration_ms": run.duration_ms, "error": run.error,
            "started_at": _timestamp(run.started_at), "finished_at": _timestamp(run.finished_at)}


@app.get("/admin/accounting-connections/{provider}/sync-history")
def accounting_master_sync_history(
    provider: str, limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    rows = session.scalars(select(MasterDataSyncRunRecord).where(
        MasterDataSyncRunRecord.provider == provider
    ).order_by(MasterDataSyncRunRecord.started_at.desc()).limit(limit)).all()
    return [master_data_sync_run_result(row) for row in rows]


@app.get("/admin/accounting-connections/{provider}/master-data")
def accounting_master_data(
    provider: str, category: str = Query(default="ALL", pattern="^(ALL|supplier|item|tax|currency)$"),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    query = select(AccountingMasterDataRecord).where(
        AccountingMasterDataRecord.provider == provider.casefold())
    if category != "ALL":
        query = query.where(AccountingMasterDataRecord.category == category)
    query = query.order_by(AccountingMasterDataRecord.category, AccountingMasterDataRecord.name)
    return [{"provider": row.provider, "category": row.category, "source_id": row.source_id,
             "name": row.name, "code": row.code, "active": row.active,
             "synced_at": _timestamp(row.synced_at)} for row in session.scalars(query).all()]


@app.get("/admin/accounting-connections/{provider}/mapping-suggestions")
def accounting_mapping_suggestions(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    if connection is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    config = decrypt_configuration(connection.encrypted_config)
    source_to_field = {"supplier": mapping_field(provider, "supplier"),
                       "item": mapping_field(provider, "item"), "tax": mapping_field(provider, "tax")}
    existing = {category: {" ".join(key.casefold().split()) for key in config.get(field, {})}
                for category, field in source_to_field.items()}
    sources: dict[str, set[str]] = {category: set() for category in source_to_field}
    for match in session.scalars(select(MatchRecord)).all():
        invoice = match.input_data.get("invoice", {})
        if invoice.get("vendor"):
            sources["supplier"].add(str(invoice["vendor"]))
        for line in invoice.get("items", []):
            if line.get("sku"):
                sources["item"].add(str(line["sku"]))
            if line.get("tax_code"):
                sources["tax"].add(str(line["tax_code"]))
    suggestions = []
    for category, labels in sources.items():
        candidates = session.scalars(select(AccountingMasterDataRecord).where(
            AccountingMasterDataRecord.provider == provider,
            AccountingMasterDataRecord.category == category,
            AccountingMasterDataRecord.active.is_(True))).all()
        for label in sorted(labels):
            if " ".join(label.casefold().split()) in existing[category]:
                continue
            norm = " ".join(label.casefold().split())
            ranked = sorted(((max(SequenceMatcher(None, norm, " ".join((row.name + " " + (row.code or "")).casefold().split())).ratio(),
                                      SequenceMatcher(None, norm, " ".join((row.code or "").casefold().split())).ratio()), row)
                             for row in candidates), key=lambda part: (part[0], part[1].source_id), reverse=True)
            if ranked and ranked[0][0] >= 0.72:
                score, row = ranked[0]
                alternatives = [{"source_id": candidate.source_id, "name": candidate.name,
                    "code": candidate.code, "confidence": round(candidate_score, 3)}
                    for candidate_score, candidate in ranked[1:3] if candidate_score >= 0.72]
                suggestions.append({"category": category, "source_label": label,
                    "suggested_name": row.name, "suggested_code": row.code,
                    "suggested_source_id": row.source_id, "confidence": round(score, 3),
                    "ambiguous": bool(alternatives and score - alternatives[0]["confidence"] <= 0.10),
                    "alternatives": alternatives,
                    "apply_automatically": False})
    decisions = session.scalars(select(MappingSuggestionDecisionRecord).where(
        MappingSuggestionDecisionRecord.provider == provider)).all()
    reviewed = {(item.category, item.source_label, item.suggested_source_id): item for item in decisions}
    for suggestion in suggestions:
        decision = reviewed.get((suggestion["category"], suggestion["source_label"],
                                 suggestion["suggested_source_id"]))
        suggestion["review_status"] = ({"DISMISS": "DISMISSED"}.get(decision.decision, decision.decision)
                                       if decision else "PENDING")
        suggestion["reviewed_by"] = decision.actor if decision else None
        suggestion["reviewed_at"] = _timestamp(decision.created_at) if decision else None
    return suggestions


@app.get("/admin/accounting-connections/{provider}/mapping-coverage")
def accounting_mapping_coverage(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    if connection is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    try:
        config = decrypt_configuration(connection.encrypted_config)
        fields = {category: mapping_field(provider, category) for category in ("supplier", "item", "tax")}
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    labels: dict[str, dict[str, int]] = {category: {} for category in fields}
    for match in session.scalars(select(MatchRecord)).all():
        invoice = match.input_data.get("invoice", {})
        vendor = invoice.get("vendor") or match.vendor
        if vendor:
            name = str(vendor).strip()
            labels["supplier"][name] = labels["supplier"].get(name, 0) + 1
        for line in invoice.get("items", []):
            for category, key in (("item", "sku"), ("tax", "tax_code")):
                value = line.get(key)
                if value:
                    name = str(value).strip()
                    labels[category][name] = labels[category].get(name, 0) + 1
    active_ids = {category: {row.source_id for row in session.scalars(select(AccountingMasterDataRecord).where(
        AccountingMasterDataRecord.provider == provider,
        AccountingMasterDataRecord.category == category,
        AccountingMasterDataRecord.active.is_(True))).all()} for category in fields}
    suggestions = accounting_mapping_suggestions(provider, session, admin)
    suggestion_by_key = {(row["category"], row["source_label"]): row for row in suggestions}
    unmapped, inactive_mappings, ambiguous, mapping_conflicts = [], [], [], []
    for category, source_labels in labels.items():
        mappings = config.get(fields[category], {})
        normalized_mappings = {" ".join(key.casefold().split()): (key, value)
                               for key, value in mappings.items()}
        for source_label, occurrences in sorted(source_labels.items()):
            mapped = normalized_mappings.get(" ".join(source_label.casefold().split()))
            if mapped is None:
                suggestion = suggestion_by_key.get((category, source_label))
                unmapped.append({"category": category, "source_label": source_label,
                    "occurrences": occurrences,
                    "suggested_source_id": suggestion["suggested_source_id"] if suggestion else None,
                    "suggested_name": suggestion["suggested_name"] if suggestion else None})
                if suggestion and suggestion["ambiguous"]:
                    ambiguous.append({"category": category, "source_label": source_label,
                        "candidates": [{"source_id": suggestion["suggested_source_id"],
                            "name": suggestion["suggested_name"], "confidence": suggestion["confidence"]},
                            *suggestion["alternatives"]]})
        for source_key, source_id in mappings.items():
            if source_id not in active_ids[category]:
                inactive_mappings.append({"category": category, "source_label": source_key,
                                          "provider_source_id": source_id})
        mapping_conflicts.extend({"category": category, **issue}
            for issue in _mapping_validation_issues(session, provider, category, mappings))
    label_count = sum(len(values) for values in labels.values())
    mapped_count = label_count - len(unmapped)
    return {"provider": provider,
        "summary": {"labels": label_count, "mapped": mapped_count,
                    "coverage_percent": round(mapped_count / label_count * 100, 1) if label_count else 100.0,
                    "unmapped": len(unmapped), "inactive_mappings": len(inactive_mappings),
                    "ambiguous_suggestions": len(ambiguous),
                    "mapping_conflicts": len(mapping_conflicts)},
        "unmapped": unmapped, "inactive_mappings": inactive_mappings,
        "ambiguous_suggestions": ambiguous, "mapping_conflicts": mapping_conflicts}


@app.post("/admin/accounting-connections/{provider}/mapping-suggestions/review")
def review_accounting_mapping_suggestion(
    provider: str, body: MappingSuggestionDecision,
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    if connection is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    current_suggestions = accounting_mapping_suggestions(provider, session, admin)
    suggestion = next((item for item in current_suggestions
        if item["category"] == body.category and item["source_label"] == body.source_label
        and item["suggested_source_id"] == body.suggested_source_id), None)
    # An accepted mapping no longer appears in the live suggestions. Treat an identical
    # repeat decision as idempotent, while rejecting decisions for stale candidates.
    previous = session.scalar(select(MappingSuggestionDecisionRecord).where(
        MappingSuggestionDecisionRecord.provider == provider,
        MappingSuggestionDecisionRecord.category == body.category,
        MappingSuggestionDecisionRecord.source_label == body.source_label,
        MappingSuggestionDecisionRecord.suggested_source_id == body.suggested_source_id))
    if previous is not None:
        if previous.decision == body.decision:
            return {"provider": provider, "decision": previous.decision, "idempotent": True}
        raise HTTPException(status_code=409, detail="This mapping suggestion has already been reviewed")
    if suggestion is None or suggestion["review_status"] != "PENDING":
        raise HTTPException(status_code=409, detail="Mapping suggestion is no longer pending")

    details = {"category": body.category, "source_label": body.source_label,
               "suggested_source_id": body.suggested_source_id}
    if body.decision == "ACCEPT":
        try:
            field = mapping_field(provider, body.category)
            config = decrypt_configuration(connection.encrypted_config)
            mappings = config.get(field, {})
            normalized = " ".join(body.source_label.casefold().split())
            if any(" ".join(key.casefold().split()) == normalized for key in mappings):
                raise HTTPException(status_code=409, detail="A mapping already exists for this source label")
            issues = _mapping_validation_issues(session, provider, body.category,
                {body.source_label: body.suggested_source_id})
            if issues:
                raise HTTPException(status_code=422, detail={"message": "Suggested provider target is not valid",
                                                              "issues": issues})
            before = dict(mappings)
            mappings[" ".join(body.source_label.strip().split())] = body.suggested_source_id
            config[field] = mappings
            validate_configuration(provider, config)
            connection.encrypted_config = encrypt_configuration(config)
            connection.updated_by = admin.name
            connection.updated_at = datetime.now(timezone.utc)
            _record_mapping_changes(session, provider=provider, category=body.category,
                before=before, after=mappings, actor=admin.name, change_type="SUGGESTION_ACCEPTED")
            details["mapping_change"] = {"before": None, "after": body.suggested_source_id}
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        event_type = "ACCOUNTING_MAPPING_SUGGESTION_ACCEPTED"
    else:
        event_type = "ACCOUNTING_MAPPING_SUGGESTION_DISMISSED"
    session.add(MappingSuggestionDecisionRecord(id=str(uuid4()), provider=provider,
        category=body.category, source_label=body.source_label,
        suggested_source_id=body.suggested_source_id, decision=body.decision, actor=admin.name))
    session.add(AccountingConnectionEventRecord(provider=provider, event_type=event_type,
        actor=admin.name, details=details))
    session.commit()
    return {"provider": provider, "decision": body.decision, "idempotent": False}


def _master_sync_status(schedule: MasterDataSyncScheduleRecord, now: datetime) -> str:
    last_success = _utc(schedule.last_success_at)
    next_run = _utc(schedule.next_run_at)
    if schedule.last_error:
        return "FAILED"
    if last_success is not None and now - last_success > timedelta(minutes=schedule.stale_after_minutes):
        return "STALE"
    if not schedule.enabled:
        return "DISABLED"
    if last_success is None:
        return "NEVER_SYNCED"
    if next_run and next_run <= now:
        return "OVERDUE"
    return "CURRENT"


@app.get("/admin/accounting-connections/sync-status")
def accounting_master_sync_status(
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    connections = session.scalars(select(AccountingConnectionRecord).order_by(
        AccountingConnectionRecord.provider)).all()
    results = []
    for connection in connections:
        schedule = session.scalar(select(MasterDataSyncScheduleRecord).where(
            MasterDataSyncScheduleRecord.provider == connection.provider))
        if schedule is None:
            schedule = MasterDataSyncScheduleRecord(id=str(uuid4()), provider=connection.provider,
                enabled=False, interval_minutes=1440, stale_after_minutes=2880,
                connection_check_enabled=True, connection_check_interval_minutes=60,
                next_connection_check_at=now)
            session.add(schedule)
            session.flush()
        health = session.scalar(select(AccountingConnectionHealthRecord).where(
            AccountingConnectionHealthRecord.provider == connection.provider))
        results.append({"provider": connection.provider, "connection_enabled": connection.enabled,
            "enabled": schedule.enabled, "interval_minutes": schedule.interval_minutes,
            "stale_after_minutes": schedule.stale_after_minutes,
            "connection_check_enabled": schedule.connection_check_enabled,
            "connection_check_interval_minutes": schedule.connection_check_interval_minutes,
            "last_connection_check_at": _timestamp(schedule.last_connection_check_at),
            "next_connection_check_at": _timestamp(schedule.next_connection_check_at),
            "sync_status": _master_sync_status(schedule, now),
            "last_started_at": _timestamp(schedule.last_started_at),
            "last_success_at": _timestamp(schedule.last_success_at),
            "next_run_at": _timestamp(schedule.next_run_at), "last_error": schedule.last_error,
            "connection_health": ({"status": health.status, "failure_type": health.failure_type,
                "account_name": health.account_name, "account_id": health.account_id,
                "error": health.error, "checked_at": _timestamp(health.checked_at),
                "reconnect_required": health.failure_type == "AUTHENTICATION",
                "reconnect_action": ("Reconnect provider credentials, then run Test connection."
                    if health.failure_type == "AUTHENTICATION" else None)} if health else None)})
    session.commit()
    return results


@app.put("/admin/accounting-connections/{provider}/sync-schedule")
def update_accounting_master_sync_schedule(
    provider: str, body: MasterDataSyncScheduleUpdate,
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    if connection is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    schedule = session.scalar(select(MasterDataSyncScheduleRecord).where(
        MasterDataSyncScheduleRecord.provider == provider))
    if schedule is None:
        schedule = MasterDataSyncScheduleRecord(id=str(uuid4()), provider=provider)
        session.add(schedule)
    schedule.enabled = body.enabled
    schedule.interval_minutes = body.interval_minutes
    schedule.stale_after_minutes = body.stale_after_minutes
    schedule.next_run_at = datetime.now(timezone.utc) if body.enabled else None
    schedule.connection_check_enabled = body.connection_check_enabled
    schedule.connection_check_interval_minutes = body.connection_check_interval_minutes
    schedule.next_connection_check_at = (datetime.now(timezone.utc)
        if body.connection_check_enabled else None)
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_MASTER_DATA_SCHEDULE_UPDATED", actor=admin.name,
        details={"enabled": body.enabled, "interval_minutes": body.interval_minutes,
                 "stale_after_minutes": body.stale_after_minutes,
                 "connection_check_enabled": body.connection_check_enabled,
                 "connection_check_interval_minutes": body.connection_check_interval_minutes}))
    session.commit()
    return {"provider": provider, "enabled": schedule.enabled,
            "interval_minutes": schedule.interval_minutes,
            "stale_after_minutes": schedule.stale_after_minutes,
            "connection_check_enabled": schedule.connection_check_enabled,
            "connection_check_interval_minutes": schedule.connection_check_interval_minutes,
            "next_run_at": _timestamp(schedule.next_run_at)}


@app.post("/admin/accounting-connections/{provider}/test-connection")
def test_accounting_connection(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    if connection is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    checked_at = datetime.now(timezone.utc)
    failure_type, error, account = None, None, {}
    try:
        if not connection.enabled:
            raise ValueError("Accounting connection is disabled")
        adapter = create_adapter(provider, company_id=session.info.get("company_id", "default"))
        check = getattr(adapter, "test_connection", None)
        if not callable(check):
            raise RuntimeError(f"The {provider} adapter does not support connection checks")
        account = check() or {}
        status = "HEALTHY"
    except Exception as exc:
        status = "FAILED"
        error = str(exc)[:1000]
        failure_type = provider_failure_type(exc)
    health = session.scalar(select(AccountingConnectionHealthRecord).where(
        AccountingConnectionHealthRecord.provider == provider))
    if health is None:
        health = AccountingConnectionHealthRecord(id=str(uuid4()), provider=provider, status=status,
            checked_at=checked_at)
        session.add(health)
    health.status, health.failure_type = status, failure_type
    health.account_name = str(account.get("account_name"))[:255] if account.get("account_name") else None
    health.account_id = str(account.get("account_id"))[:160] if account.get("account_id") else None
    health.error, health.checked_at = error, checked_at
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_CONNECTION_TESTED", actor=admin.name,
        details={"status": status, "failure_type": failure_type, "error": error,
                 "account_id": health.account_id, "account_name": health.account_name}))
    schedule = session.scalar(select(MasterDataSyncScheduleRecord).where(
        MasterDataSyncScheduleRecord.provider == provider))
    if schedule is not None:
        schedule.last_connection_check_at = checked_at
        schedule.next_connection_check_at = (checked_at + timedelta(
            minutes=schedule.connection_check_interval_minutes)
            if schedule.connection_check_enabled else None)
    session.commit()
    return {"provider": provider, "status": status, "failure_type": failure_type,
            "account_name": health.account_name, "account_id": health.account_id,
            "error": error, "checked_at": _timestamp(checked_at),
            "reconnect_required": failure_type == "AUTHENTICATION",
            "reconnect_action": ("Reconnect the provider from Admin → Accounting connections, then run Test connection."
                if failure_type == "AUTHENTICATION" else None)}


@app.get("/admin/accounting-connections/{provider}/mappings")
def get_accounting_mappings(
    provider: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    record = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    try:
        config = decrypt_configuration(record.encrypted_config)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    categories = {category: config.get(field, {}) for category, field in (
        ("supplier", mapping_field(provider, "supplier")),
        ("item", mapping_field(provider, "item")),
        ("tax", mapping_field(provider, "tax")),
        ("currency", mapping_field(provider, "currency")),
    )}
    return {"provider": provider, "enabled": record.enabled, "mappings": categories}


def _record_mapping_changes(session: Session, *, provider: str, category: str,
                            before: dict[str, str], after: dict[str, str], actor: str,
                            change_type: str, reverts_change_id: str | None = None) -> list[str]:
    normalize = lambda key: " ".join(key.casefold().split())
    old = {normalize(key): (key, value) for key, value in before.items()}
    new = {normalize(key): (key, value) for key, value in after.items()}
    created = []
    for normalized_key in sorted(old.keys() | new.keys()):
        previous = old.get(normalized_key)
        current = new.get(normalized_key)
        if previous == current:
            continue
        revision_id = str(uuid4())
        session.add(AccountingMappingChangeRecord(id=revision_id, provider=provider,
            category=category, source_label=(current or previous)[0],
            previous_source_id=previous[1] if previous else None,
            previous_source_label=previous[0] if previous else None,
            new_source_id=current[1] if current else None,
            new_source_label=current[0] if current else None,
            change_type=change_type, actor=actor, reverts_change_id=reverts_change_id))
        created.append(revision_id)
    return created


def _mapping_validation_issues(session: Session, provider: str, category: str,
                               mappings: dict[str, str]) -> list[dict]:
    records = session.scalars(select(AccountingMasterDataRecord).where(
        AccountingMasterDataRecord.provider == provider,
        AccountingMasterDataRecord.category == category)).all()
    by_id = {row.source_id: row for row in records}
    issues = []
    targets: dict[str, list[str]] = {}
    for label, source_id in mappings.items():
        target = by_id.get(source_id)
        if target is None or not target.active:
            issues.append({"type": "UNKNOWN_OR_INACTIVE_TARGET", "source_label": label,
                           "provider_source_id": source_id,
                           "provider_name": target.name if target else None,
                           "active": target.active if target else False})
        targets.setdefault(source_id, []).append(label)
    for source_id, labels in targets.items():
        if len(labels) > 1:
            issues.append({"type": "DUPLICATE_PROVIDER_TARGET", "provider_source_id": source_id,
                           "source_labels": sorted(labels),
                           "provider_name": by_id[source_id].name if source_id in by_id else None})
    return issues


def mapping_change_result(change: AccountingMappingChangeRecord) -> dict:
    return {"id": change.id, "provider": change.provider, "category": change.category,
            "source_label": change.source_label,
            "previous_source_id": change.previous_source_id,
            "previous_source_label": change.previous_source_label,
            "new_source_id": change.new_source_id, "new_source_label": change.new_source_label,
            "change_type": change.change_type, "actor": change.actor,
            "reverts_change_id": change.reverts_change_id,
            "created_at": _timestamp(change.created_at)}


@app.get("/admin/accounting-connections/{provider}/mapping-history")
def accounting_mapping_history(
    provider: str, limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    rows = session.scalars(select(AccountingMappingChangeRecord).where(
        AccountingMappingChangeRecord.provider == provider.casefold()
    ).order_by(AccountingMappingChangeRecord.created_at.desc()).limit(limit)).all()
    return [mapping_change_result(row) for row in rows]


@app.post("/admin/accounting-connections/{provider}/mapping-history/{change_id}/rollback")
def rollback_accounting_mapping_change(
    provider: str, change_id: str, session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    provider = provider.casefold()
    connection = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider))
    change = session.scalar(select(AccountingMappingChangeRecord).where(
        AccountingMappingChangeRecord.id == change_id,
        AccountingMappingChangeRecord.provider == provider))
    if connection is None or change is None:
        raise HTTPException(status_code=404, detail="Mapping change not found")
    try:
        field = mapping_field(provider, change.category)
        config = decrypt_configuration(connection.encrypted_config)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    mappings = config.get(field, {})
    normalize = lambda key: " ".join(key.casefold().split())
    key = next((candidate for candidate in mappings
                if normalize(candidate) == normalize(change.source_label)), None)
    current = (key, mappings[key]) if key is not None else None
    expected = ((change.new_source_label, change.new_source_id)
                if change.new_source_id is not None else None)
    if current != expected:
        raise HTTPException(status_code=409,
            detail="Mapping has changed since this revision; refresh history before rollback")
    before = dict(mappings)
    if key is not None:
        del mappings[key]
    if change.previous_source_id is not None and change.previous_source_label is not None:
        mappings[change.previous_source_label] = change.previous_source_id
    issues = _mapping_validation_issues(session, provider, change.category, mappings)
    if issues:
        raise HTTPException(status_code=422, detail={"message": "Previous mapping target is no longer valid",
                                                      "issues": issues})
    config[field] = mappings
    try:
        validate_configuration(provider, config)
        connection.encrypted_config = encrypt_configuration(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _record_mapping_changes(session, provider=provider, category=change.category,
        before=before, after=mappings, actor=admin.name, change_type="ROLLBACK",
        reverts_change_id=change.id)
    connection.updated_by = admin.name
    connection.updated_at = datetime.now(timezone.utc)
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_MAPPING_ROLLED_BACK", actor=admin.name,
        details={"change_id": change.id, "category": change.category,
                 "source_label": change.source_label}))
    session.commit()
    return {"provider": provider, "rolled_back_change_id": change.id,
            "category": change.category, "source_label": change.source_label}


@app.put("/admin/accounting-connections/{provider}/mappings/{category}")
def replace_accounting_mapping(
    provider: str, category: str, body: AccountingMappingUpdate,
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider, category = provider.casefold(), category.casefold()
    try:
        field = mapping_field(provider, category)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    try:
        config = decrypt_configuration(record.encrypted_config)
        before = config.get(field, {})
        after = {" ".join(key.strip().split()): value.strip()
                 for key, value in body.mappings.items()}
        issues = _mapping_validation_issues(session, provider, category, after)
        if issues:
            raise HTTPException(status_code=422, detail={"message": "Mapping validation failed",
                                                          "issues": issues})
        config[field] = after
        validate_configuration(provider, config)
        record.encrypted_config = encrypt_configuration(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record.updated_by = admin.name
    record.updated_at = datetime.now(timezone.utc)
    _record_mapping_changes(session, provider=provider, category=category, before=before,
        after=after, actor=admin.name, change_type="REPLACED")
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_MAPPING_REPLACED", actor=admin.name,
        details={"category": category, "mapping_count": len(body.mappings)}))
    session.commit()
    return {"provider": provider, "category": category, "mapping_count": len(body.mappings)}


@app.delete("/admin/accounting-connections/{provider}/mappings/{category}/{source_key}")
def delete_accounting_mapping(
    provider: str, category: str, source_key: str,
    session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    provider, category = provider.casefold(), category.casefold()
    try:
        field = mapping_field(provider, category)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record = session.scalar(select(AccountingConnectionRecord).where(
        AccountingConnectionRecord.provider == provider
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="Accounting connection not found")
    try:
        config = decrypt_configuration(record.encrypted_config)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    mapping = config.get(field, {})
    normalized_key = " ".join(source_key.casefold().split())
    key = next((candidate for candidate in mapping if " ".join(candidate.casefold().split()) == normalized_key), None)
    if key is None:
        raise HTTPException(status_code=404, detail="Mapping not found")
    before = dict(mapping)
    del mapping[key]
    config[field] = mapping
    record.encrypted_config = encrypt_configuration(config)
    record.updated_by = admin.name
    record.updated_at = datetime.now(timezone.utc)
    _record_mapping_changes(session, provider=provider, category=category, before=before,
        after=mapping, actor=admin.name, change_type="DELETED")
    session.add(AccountingConnectionEventRecord(provider=provider,
        event_type="ACCOUNTING_MAPPING_DELETED", actor=admin.name,
        details={"category": category}))
    session.commit()
    return {"provider": provider, "category": category, "deleted": True}


@app.get("/reviewer/ap-outbox/{match_id}/mapping-check")
def ap_outbox_mapping_check(
    match_id: str, session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    item = session.get(APOutboxRecord, match_id)
    if item is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    try:
        adapter = create_adapter_for_company(session.info.get("company_id", "default"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"AP delivery adapter is unavailable: {exc}") from exc
    issues = check_payload_mappings(adapter, item.payload)
    return {"match_id": match_id, "provider": adapter.name, "ready": not issues,
            "issues": issues}


@app.get("/reviewer/ap-outbox/{match_id}/history")
def ap_outbox_retry_history(
    match_id: str, session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    if session.get(APOutboxRecord, match_id) is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    events = session.scalars(select(AuditEvent).where(
        AuditEvent.match_id == match_id,
        AuditEvent.event_type.in_({"AP_EXPORT_RETRY_REQUESTED", "AP_EXPORT_FAILED",
                                   "AP_EXPORT_ACKNOWLEDGED", "AP_EXPORT_REFERENCE_RECONCILED",
                                   "AP_EXPORT_BLOCKED_MISSING_MAPPING",
                                   "AP_ACCOUNTING_STATUS_UPDATED", "AP_ACCOUNTING_STATUS_SYNC_FAILED"}),
    ).order_by(AuditEvent.created_at.desc()).limit(100)).all()
    return [{"event_type": event.event_type, "actor": event.actor,
             "details": event.details, "created_at": _timestamp(event.created_at)} for event in events]


@app.post("/reviewer/ap-outbox/{match_id}/sync-status", response_model=ReviewerAPOutboxItem)
def sync_accounting_status(
    match_id: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    item = session.get(APOutboxRecord, match_id)
    match = session.get(MatchRecord, match_id)
    if item is None or match is None:
        raise HTTPException(status_code=404, detail="AP outbox item not found")
    if item.state != "ACKNOWLEDGED" or not item.external_reference:
        raise HTTPException(status_code=409, detail="Invoice must be acknowledged and have an external reference")
    try:
        adapter = create_adapter_for_company(session.info.get("company_id", "default"))
        refresh_accounting_status(session, adapter, item, actor=admin.name)
    except Exception as exc:
        session.commit()
        raise HTTPException(status_code=502, detail={"message": f"Accounting status lookup failed: {exc}",
            "retry_at": _timestamp(item.accounting_status_next_retry_at),
            "attempt_count": item.accounting_status_attempt_count}) from exc
    session.commit()
    return ReviewerAPOutboxItem(
        match_id=item.match_id, state=item.state, vendor=match.vendor, invoice_number=match.invoice_number,
        currency=str(item.payload.get("currency") or "USD"), total_amount=item.payload.get("total_amount", 0),
        created_at=_timestamp(item.created_at), acknowledged_at=_timestamp(item.acknowledged_at),
        attempt_count=item.attempt_count, next_attempt_at=_timestamp(item.next_attempt_at),
        last_error=item.last_error, external_reference=item.external_reference,
        accounting_status=item.accounting_status, accounting_status_detail=item.accounting_status_detail,
        accounting_status_checked_at=_timestamp(item.accounting_status_checked_at),
        accounting_status_attempt_count=item.accounting_status_attempt_count,
        accounting_status_next_retry_at=_timestamp(item.accounting_status_next_retry_at),
        accounting_status_error=item.accounting_status_error,
    )


@app.post("/integrations/bank-statements/import", status_code=201)
def import_bank_statement(
    body: BankStatementImport, session: Session = Depends(get_session),
    integration: str = Depends(require_integration),
):
    content_hash = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    batch = session.scalar(select(BankStatementBatchRecord).where(
        BankStatementBatchRecord.bank_account_id == body.account_id,
        BankStatementBatchRecord.statement_id == body.statement_id,
    ))
    if batch is not None:
        if (batch.content_sha256 != content_hash or batch.format != body.format
                or batch.opening_balance != body.opening_balance
                or batch.closing_balance != body.closing_balance):
            raise HTTPException(status_code=409, detail={"code": "BANK_STATEMENT_ID_REUSED"})
        return {"statement_id": batch.statement_id, "account_id": batch.bank_account_id,
                "format": batch.format, "transactions_seen": batch.transaction_count,
                "imported": 0, "duplicates": batch.imported_count + batch.duplicate_count,
                "non_debit_skipped": batch.skipped_count, "batch_id": batch.id,
                "idempotent_replay": True, "payments": []}
    configuration = session.scalar(select(BankAccountConfigurationRecord).where(
        BankAccountConfigurationRecord.bank_account_id == body.account_id
    ))
    try:
        transactions = parse_statement(body.format, body.content,
            bai2_transaction_codes=configuration.bai2_transaction_codes if configuration else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    batch = BankStatementBatchRecord(id=str(uuid4()), bank_account_id=body.account_id,
        statement_id=body.statement_id, format=body.format, content_sha256=content_hash,
        opening_balance=body.opening_balance, closing_balance=body.closing_balance,
        currency=body.currency or (transactions[0]["currency"] if transactions else None),
        transaction_count=len(transactions), imported_count=0, duplicate_count=0, skipped_count=0)
    session.add(batch)
    session.flush()
    imported, duplicates, skipped = [], 0, 0
    for transaction in transactions:
        # The existing remittance table intentionally models outgoing payments to suppliers.
        if transaction["direction"] != "DEBIT":
            skipped += 1
            continue
        external_id = f"bank:{body.account_id}:{transaction['external_id']}"
        existing = session.scalar(select(RemittancePaymentRecord).where(
            RemittancePaymentRecord.bank_account_id == body.account_id,
            RemittancePaymentRecord.bank_transaction_id == transaction["external_id"]))
        if existing is not None:
            prior = existing.input_data.get("bank_transaction", {})
            same = (Decimal(str(existing.amount)) == transaction["amount"]
                    and existing.currency == transaction["currency"]
                    and str(prior.get("reference") or "") == str(transaction["reference"] or ""))
            if not same:
                raise HTTPException(status_code=409, detail={
                    "code": "BANK_TRANSACTION_ID_REUSED", "transaction_id": transaction["external_id"]})
            duplicates += 1
            continue
        bank_transaction = {**transaction, "amount": str(transaction["amount"]),
                            "payment_date": transaction["payment_date"].isoformat()}
        input_data = {"source": "bank_statement", "statement_id": body.statement_id,
                      "format": body.format, "bank_account_id": body.account_id,
                      "bank_statement_batch_id": batch.id, "bank_transaction": bank_transaction}
        payment = RemittancePaymentRecord(id=str(uuid4()), external_id=external_id,
            vendor=transaction["vendor"], currency=transaction["currency"], amount=transaction["amount"],
            reference=transaction["reference"], payment_date=transaction["payment_date"],
            status="NEEDS_REVIEW", input_data=input_data, allocated_amount=Decimal(0),
            bank_account_id=body.account_id, bank_transaction_id=transaction["external_id"],
            bank_statement_batch_id=batch.id)
        session.add(payment)
        session.flush()
        session.add(PaymentReconciliationEventRecord(payment_id=payment.id,
            event_type="BANK_TRANSACTION_IMPORTED", actor=integration,
            details={"statement_id": body.statement_id, "format": body.format,
                     "transaction_id": transaction["external_id"],
                     "description": transaction["description"], "bank_account_id": body.account_id}))
        imported.append(payment)
    batch.imported_count, batch.duplicate_count, batch.skipped_count = len(imported), duplicates, skipped
    session.add(AccountingConnectionEventRecord(provider="bank", event_type="BANK_STATEMENT_IMPORTED",
        actor=integration, details={"statement_id": body.statement_id, "bank_account_id": body.account_id,
                                    "batch_id": batch.id, "format": body.format,
                                    "imported": len(imported), "duplicates": duplicates,
                                    "non_debit_skipped": skipped}))
    session.commit()
    return {"statement_id": body.statement_id, "account_id": body.account_id, "batch_id": batch.id,
            "opening_balance": str(body.opening_balance) if body.opening_balance is not None else None,
            "closing_balance": str(body.closing_balance) if body.closing_balance is not None else None,
            "format": body.format,
            "transactions_seen": len(transactions), "imported": len(imported),
            "duplicates": duplicates, "non_debit_skipped": skipped,
            "payments": [payment_result(session, payment) for payment in imported]}


@app.get("/reviewer/bank-transactions")
def reviewer_bank_transactions(
    status: str = Query(default="ALL", pattern="^(NEEDS_REVIEW|MATCHED|PARTIALLY_MATCHED|ALL)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    query = select(RemittancePaymentRecord).where(
        RemittancePaymentRecord.external_id.like("bank:%"))
    if status != "ALL":
        query = query.where(RemittancePaymentRecord.status == status)
    rows = session.scalars(query.order_by(RemittancePaymentRecord.created_at.desc()).limit(limit)).all()
    return [payment_result(session, row) for row in rows]


@app.get("/reviewer/bank-transactions/{payment_id}/candidates")
def bank_reconciliation_candidates(
    payment_id: str, session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    payment = session.get(RemittancePaymentRecord, payment_id)
    if payment is None or payment.bank_account_id is None:
        raise HTTPException(status_code=404, detail="Bank transaction not found")
    reference = " ".join(str(payment.reference or "").casefold().split())
    candidates = []
    for outbox, match, outstanding in _payment_candidates(session, payment):
        reasons = [{"type": "vendor", "score": 30,
                    "detail": f"Supplier matches {match.vendor}"}]
        invoice_ref = " ".join(str(match.invoice_number or "").casefold().split())
        external_ref = " ".join(str(outbox.external_reference or "").casefold().split())
        if reference and reference in {invoice_ref, external_ref, match.id.casefold()}:
            reasons.append({"type": "reference", "score": 50,
                            "detail": f"Bank reference matches {match.invoice_number or outbox.external_reference or match.id}"})
        elif reference and invoice_ref and (reference in invoice_ref or invoice_ref in reference):
            reasons.append({"type": "reference", "score": 25,
                            "detail": "Bank reference partially matches the invoice number"})
        amount_gap = abs(outstanding - (payment.amount - Decimal(str(payment.allocated_amount or 0))))
        if amount_gap == 0:
            reasons.append({"type": "amount", "score": 35, "detail": "Outstanding invoice balance equals the unallocated bank amount"})
        elif outstanding >= payment.amount - Decimal(str(payment.allocated_amount or 0)):
            reasons.append({"type": "amount", "score": 15, "detail": "Invoice has enough outstanding balance for this payment"})
        invoice = match.input_data.get("invoice", {})
        invoice_date = invoice.get("invoice_date")
        payment_date = payment.payment_date
        try:
            date_delta = abs((payment_date.date() - date.fromisoformat(str(invoice_date)[:10])).days)
        except (ValueError, TypeError, AttributeError):
            date_delta = None
        if date_delta is not None and date_delta <= 45:
            reasons.append({"type": "date", "score": max(5, 20 - date_delta // 3),
                            "detail": f"Invoice date is {date_delta} day(s) from the bank transaction"})
        candidates.append({"match_id": match.id, "invoice_number": match.invoice_number,
            "vendor": match.vendor, "currency": payment.currency, "outstanding_amount": str(outstanding),
            "score": sum(reason["score"] for reason in reasons), "reasons": reasons})
    candidates.sort(key=lambda item: (-item["score"], item["invoice_number"] or ""))
    payment_candidates = []
    if payment.reference:
        reference_key = " ".join(payment.reference.casefold().split())
    else:
        reference_key = ""
    remittances = session.scalars(select(RemittancePaymentRecord).where(
        RemittancePaymentRecord.bank_account_id.is_(None),
        RemittancePaymentRecord.currency == payment.currency,
    ).order_by(RemittancePaymentRecord.created_at.desc()).limit(1000)).all()
    for remittance in remittances:
        if " ".join(remittance.vendor.casefold().split()) != " ".join(payment.vendor.casefold().split()):
            continue
        if session.scalar(select(RemittancePaymentRecord.id).where(
                RemittancePaymentRecord.matched_remittance_id == remittance.id).limit(1)):
            continue
        reasons = [{"type": "vendor", "score": 30,
                    "detail": f"Supplier matches {remittance.vendor}"}]
        remittance_refs = {" ".join(str(value or "").casefold().split())
                           for value in (remittance.reference, remittance.external_id) if value}
        if reference_key and reference_key in remittance_refs:
            reasons.append({"type": "reference", "score": 50, "detail": "Bank reference matches payment advice"})
        amount_gap = abs(Decimal(str(remittance.amount))
                         - (Decimal(str(payment.amount)) - Decimal(str(payment.allocated_amount or 0))))
        if amount_gap == 0:
            reasons.append({"type": "amount", "score": 35, "detail": "Payment advice amount equals the unallocated bank amount"})
        elif amount_gap <= Decimal("1.00"):
            reasons.append({"type": "amount", "score": 10, "detail": f"Payment advice differs by {amount_gap} {payment.currency}"})
        if payment.payment_date and remittance.payment_date:
            date_gap = abs((payment.payment_date.date() - remittance.payment_date.date()).days)
            if date_gap <= 45:
                reasons.append({"type": "date", "score": max(5, 20 - date_gap // 3),
                                "detail": f"Payment advice date is {date_gap} day(s) from bank transaction"})
        payment_candidates.append({"payment_id": remittance.id, "external_id": remittance.external_id,
            "reference": remittance.reference, "vendor": remittance.vendor,
            "currency": remittance.currency, "amount": str(remittance.amount),
            "status": remittance.status, "score": sum(reason["score"] for reason in reasons),
            "reasons": reasons})
    payment_candidates.sort(key=lambda item: (-item["score"], item["external_id"]))
    return {"payment_id": payment.id, "bank_account_id": payment.bank_account_id,
            "transaction_amount": str(payment.amount), "allocated_amount": str(payment.allocated_amount),
            "candidates": candidates, "payment_candidates": payment_candidates}


@app.post("/reviewer/bank-transactions/{payment_id}/decision")
def decide_bank_reconciliation(
    payment_id: str, body: BankReconciliationDecision, session: Session = Depends(get_session),
    approver: Principal = Depends(require_approver),
):
    payment = session.get(RemittancePaymentRecord, payment_id)
    if payment is None or payment.bank_account_id is None:
        raise HTTPException(status_code=404, detail="Bank transaction not found")
    if body.action == "reject":
        payment.input_data = {**payment.input_data, "bank_reconciliation_decision": "REJECTED"}
        session.add(PaymentReconciliationEventRecord(payment_id=payment.id,
            event_type="BANK_MATCH_REJECTED", actor=approver.name,
            details={"bank_account_id": payment.bank_account_id, "reference": payment.reference,
                     "amount": str(payment.amount), "reason": "Reviewer rejected suggested allocations"}))
        session.commit()
        return payment_result(session, payment)
    if body.action == "link_payment":
        remittance = session.get(RemittancePaymentRecord, body.payment_match_id)
        if remittance is None or remittance.bank_account_id is not None:
            raise HTTPException(status_code=404, detail="Payment advice was not found")
        if (" ".join(remittance.vendor.casefold().split()) != " ".join(payment.vendor.casefold().split())
                or remittance.currency != payment.currency
                or remittance.amount != payment.amount - Decimal(str(payment.allocated_amount or 0))):
            raise HTTPException(status_code=409, detail="Payment advice does not match supplier, currency, and amount")
        linked = session.scalar(select(RemittancePaymentRecord.id).where(
            RemittancePaymentRecord.matched_remittance_id == remittance.id).limit(1))
        if linked is not None:
            raise HTTPException(status_code=409, detail="Payment advice is already linked to a bank transaction")
        payment.matched_remittance_id = remittance.id
        payment.status = "MATCHED"
        payment.input_data = {**payment.input_data, "bank_reconciliation_decision": "PAYMENT_LINKED"}
        session.add(PaymentReconciliationEventRecord(payment_id=payment.id,
            event_type="BANK_PAYMENT_MATCH_CONFIRMED", actor=approver.name,
            details={"bank_account_id": payment.bank_account_id,
                "remittance_payment_id": remittance.id, "reference": payment.reference,
                "amount": str(payment.amount), "score_source": "reviewer-confirmed"}))
        session.commit()
        return payment_result(session, payment)
    try:
        manually_allocate(session, payment,
            [(item.match_id, item.amount) for item in body.allocations], actor=approver.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    event_type = "BANK_MATCH_CONFIRMED" if body.action == "confirm" else "BANK_MATCH_SPLIT_CONFIRMED"
    session.add(PaymentReconciliationEventRecord(payment_id=payment.id, event_type=event_type,
        actor=approver.name, details={"bank_account_id": payment.bank_account_id,
            "allocations": [{"match_id": item.match_id, "amount": str(item.amount)}
                            for item in body.allocations], "status": payment.status}))
    session.commit()
    return payment_result(session, payment)


@app.get("/reviewer/bank-statements")
def reviewer_bank_statements(
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    batches = session.scalars(select(BankStatementBatchRecord).order_by(
        BankStatementBatchRecord.created_at.desc()).limit(limit)).all()
    return [{"id": row.id, "statement_id": row.statement_id, "account_id": row.bank_account_id,
             "format": row.format, "opening_balance": str(row.opening_balance) if row.opening_balance is not None else None,
             "closing_balance": str(row.closing_balance) if row.closing_balance is not None else None,
             "currency": row.currency, "transaction_count": row.transaction_count,
             "imported_count": row.imported_count, "duplicate_count": row.duplicate_count,
             "skipped_count": row.skipped_count, "created_at": _timestamp(row.created_at)} for row in batches]


@app.put("/admin/bank-accounts/{account_id}/bai2-codes")
def configure_bai2_codes(
    account_id: str, body: Bai2TransactionCodeUpdate, session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    account_id = account_id.strip()
    if not account_id:
        raise HTTPException(status_code=422, detail="Bank account ID cannot be empty")
    record = session.scalar(select(BankAccountConfigurationRecord).where(
        BankAccountConfigurationRecord.bank_account_id == account_id))
    if record is None:
        record = BankAccountConfigurationRecord(id=str(uuid4()), bank_account_id=account_id,
            bai2_transaction_codes=body.transaction_codes, updated_by=admin.name)
        session.add(record)
    else:
        record.bai2_transaction_codes = body.transaction_codes
        record.updated_by = admin.name
        record.updated_at = datetime.now(timezone.utc)
    session.add(AccountingConnectionEventRecord(provider="bank", event_type="BAI2_CODES_UPDATED",
        actor=admin.name, details={"bank_account_id": account_id,
                                  "transaction_codes": body.transaction_codes}))
    session.commit()
    return {"account_id": account_id, "transaction_codes": record.bai2_transaction_codes,
            "updated_by": record.updated_by, "updated_at": _timestamp(record.updated_at)}


@app.get("/admin/bank-accounts/{account_id}/bai2-codes")
def get_bai2_codes(
    account_id: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    record = session.scalar(select(BankAccountConfigurationRecord).where(
        BankAccountConfigurationRecord.bank_account_id == account_id))
    return {"account_id": account_id, "transaction_codes": record.bai2_transaction_codes if record else {}}


@app.post("/integrations/payments/remittances", status_code=201)
def ingest_remittance_payment(
    body: RemittancePaymentCreate, session: Session = Depends(get_session),
    integration: str = Depends(require_integration),
):
    existing = session.scalar(select(RemittancePaymentRecord).where(
        RemittancePaymentRecord.external_id == body.external_id
    ))
    if existing is not None:
        if (existing.vendor.casefold() != body.vendor.casefold()
                or existing.currency != body.currency
                or existing.amount != body.amount):
            raise HTTPException(status_code=409, detail={"code": "PAYMENT_EXTERNAL_ID_REUSED"})
        return payment_result(session, existing)
    payment = RemittancePaymentRecord(
        id=str(uuid4()), external_id=body.external_id, vendor=body.vendor, currency=body.currency,
        amount=body.amount, reference=body.reference, payment_date=body.payment_date,
        status="NEEDS_REVIEW", input_data=body.model_dump(mode="json"), allocated_amount=Decimal(0),
    )
    session.add(payment)
    session.flush()
    reconcile_payment(session, payment, actor=integration)
    session.commit()
    session.refresh(payment)
    return payment_result(session, payment)


def _applied_credit_total(session: Session, match_id: str) -> Decimal:
    value = session.scalar(select(func.sum(CreditNoteApplicationRecord.amount)).where(
        CreditNoteApplicationRecord.invoice_match_id == match_id))
    return Decimal(str(value or 0))


def _credit_applied_total(session: Session, match_id: str) -> Decimal:
    value = session.scalar(select(func.sum(CreditNoteApplicationRecord.amount)).where(
        CreditNoteApplicationRecord.credit_match_id == match_id))
    return Decimal(str(value or 0))


@app.get("/reviewer/credit-notes")
def reviewer_credit_notes(
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    rows = session.execute(select(MatchRecord, APOutboxRecord).join(
        APOutboxRecord, APOutboxRecord.match_id == MatchRecord.id
    ).where(APOutboxRecord.state == "ACKNOWLEDGED")).all()
    result = []
    for match, outbox in rows:
        invoice = match.input_data.get("invoice", {})
        if invoice.get("document_type") != "CREDIT_NOTE":
            continue
        face = abs(Decimal(str(outbox.payload.get("total_amount") or 0)))
        applied = _credit_applied_total(session, match.id)
        result.append({"match_id": match.id, "credit_note_number": match.invoice_number,
            "vendor": match.vendor, "currency": outbox.payload.get("currency", "USD"),
            "credit_note_for": invoice.get("credit_note_for"), "total_amount": str(face),
            "applied_amount": str(applied), "remaining_amount": str(max(face - applied, Decimal(0))),
            "approval_status": match.approval_status, "accounting_status": outbox.accounting_status})
    return result


@app.post("/reviewer/credit-notes/{credit_match_id}/apply")
def apply_credit_note(
    credit_match_id: str, body: CreditNoteApplication, session: Session = Depends(get_session),
    approver: Principal = Depends(require_approver),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
):
    credit, invoice = session.get(MatchRecord, credit_match_id), session.get(MatchRecord, body.invoice_match_id)
    credit_outbox = session.get(APOutboxRecord, credit_match_id)
    invoice_outbox = session.get(APOutboxRecord, body.invoice_match_id)
    if not all((credit, invoice, credit_outbox, invoice_outbox)):
        raise HTTPException(status_code=404, detail="Credit note or invoice was not found")
    source_credit = credit.input_data.get("invoice", {})
    source_invoice = invoice.input_data.get("invoice", {})
    if source_credit.get("document_type") != "CREDIT_NOTE":
        raise HTTPException(status_code=409, detail="The selected document is not a credit note")
    if source_invoice.get("document_type", "INVOICE") != "INVOICE":
        raise HTTPException(status_code=409, detail="Credits can only be applied to supplier invoices")
    if credit.approval_status != "APPROVED":
        raise HTTPException(status_code=409, detail="Credit note requires approval before application")
    if credit_outbox.state != "ACKNOWLEDGED" or invoice_outbox.state != "ACKNOWLEDGED":
        raise HTTPException(status_code=409, detail="Both documents must be exported before applying a credit")
    links_to = str(source_credit.get("credit_note_for") or "").strip().casefold()
    if links_to not in {str(invoice.invoice_number or "").casefold(), invoice.id.casefold()}:
        raise HTTPException(status_code=409, detail="Credit note is not linked to this invoice")
    if credit.vendor_key != invoice.vendor_key:
        raise HTTPException(status_code=409, detail="Credit note and invoice suppliers do not match")
    credit_currency = str(credit_outbox.payload.get("currency") or "USD").upper()
    invoice_currency = str(invoice_outbox.payload.get("currency") or "USD").upper()
    if credit_currency != invoice_currency:
        raise HTTPException(status_code=409, detail="Credit note and invoice currencies do not match")
    if idempotency_key:
        prior_sync = session.scalar(select(CreditApplicationOutboxRecord).where(
            CreditApplicationOutboxRecord.idempotency_key == idempotency_key))
        if prior_sync is not None:
            prior_app = session.get(CreditNoteApplicationRecord, prior_sync.application_id)
            if prior_app is None or (prior_app.credit_match_id, prior_app.invoice_match_id) != (credit.id, invoice.id):
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED"})
            if Decimal(str(prior_sync.amount)) != body.amount:
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_KEY_REUSED"})
            return {"credit_match_id": credit.id, "invoice_match_id": invoice.id,
                "amount": str(prior_sync.amount), "currency": credit_currency,
                "application_id": prior_app.id, "sync_id": prior_sync.id,
                "sync_state": prior_sync.state, "idempotency_key": prior_sync.idempotency_key,
                "replayed": True}
    credit_remaining = abs(Decimal(str(credit_outbox.payload.get("total_amount") or 0))) - _credit_applied_total(session, credit.id)
    allocated = session.scalar(select(func.sum(PaymentAllocationRecord.amount)).where(
        PaymentAllocationRecord.match_id == invoice.id))
    invoice_remaining = Decimal(str(invoice_outbox.payload.get("total_amount") or 0)) \
        - Decimal(str(allocated or 0)) - _applied_credit_total(session, invoice.id)
    if body.amount > credit_remaining or body.amount > invoice_remaining:
        raise HTTPException(status_code=409, detail={"code": "CREDIT_APPLICATION_EXCEEDS_BALANCE",
            "credit_remaining": str(max(credit_remaining, Decimal(0))),
            "invoice_remaining": str(max(invoice_remaining, Decimal(0)))})
    application = session.scalar(select(CreditNoteApplicationRecord).where(
        CreditNoteApplicationRecord.credit_match_id == credit.id,
        CreditNoteApplicationRecord.invoice_match_id == invoice.id))
    if application is None:
        application = CreditNoteApplicationRecord(id=str(uuid4()), credit_match_id=credit.id,
            invoice_match_id=invoice.id, amount=body.amount, note=body.note, approved_by=approver.name)
        session.add(application)
    else:
        application.amount = Decimal(str(application.amount)) + body.amount
        application.note, application.approved_by = body.note, approver.name
    details = {"credit_match_id": credit.id, "invoice_match_id": invoice.id,
               "amount": str(body.amount), "currency": credit_currency,
               "note": body.note, "application_id": application.id}
    sync_item = CreditApplicationOutboxRecord(id=str(uuid4()), application_id=application.id,
        amount=body.amount, idempotency_key=idempotency_key or str(uuid4()),
        state="PENDING", attempt_count=0)
    session.add(sync_item)
    session.add(AuditEvent(match_id=credit.id, event_type="CREDIT_NOTE_APPLIED", actor=approver.name,
                           details=details))
    session.add(AuditEvent(match_id=invoice.id, event_type="CREDIT_NOTE_RECEIVED", actor=approver.name,
                           details=details))
    session.commit()
    return {**details, "sync_state": sync_item.state, "sync_id": sync_item.id,
            "idempotency_key": sync_item.idempotency_key, "replayed": False,
            "credit_remaining": str(credit_remaining - body.amount),
            "invoice_remaining": str(invoice_remaining - body.amount),
            "approved_by": approver.name, "created_at": _timestamp(application.created_at)}


@app.get("/reviewer/credit-notes/{credit_match_id}/applications")
def credit_note_applications(
    credit_match_id: str, session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    if session.get(MatchRecord, credit_match_id) is None:
        raise HTTPException(status_code=404, detail="Credit note not found")
    rows = session.scalars(select(CreditNoteApplicationRecord).where(
        CreditNoteApplicationRecord.credit_match_id == credit_match_id
    ).order_by(CreditNoteApplicationRecord.created_at)).all()
    result = []
    for row in rows:
        sync_rows = session.scalars(select(CreditApplicationOutboxRecord).where(
            CreditApplicationOutboxRecord.application_id == row.id
        ).order_by(CreditApplicationOutboxRecord.created_at)).all()
        latest = sync_rows[-1] if sync_rows else None
        result.append({"id": row.id, "credit_match_id": row.credit_match_id,
            "invoice_match_id": row.invoice_match_id, "amount": row.amount, "note": row.note,
            "approved_by": row.approved_by, "created_at": _timestamp(row.created_at),
            "sync_state": latest.state if latest else "NOT_QUEUED",
            "attempt_count": sum(item.attempt_count for item in sync_rows),
            "last_error": latest.last_error if latest else None,
            "next_attempt_at": _timestamp(latest.next_attempt_at) if latest else None,
            "sync_history": [{"id": item.id, "state": item.state, "amount": str(item.amount),
                "attempt_count": item.attempt_count, "last_error": item.last_error,
                "next_attempt_at": _timestamp(item.next_attempt_at),
                "external_reference": item.external_reference,
                "created_at": _timestamp(item.created_at),
                "acknowledged_at": _timestamp(item.acknowledged_at)} for item in sync_rows]})
    return result


def sync_credit_applications(session: Session, adapter, *, limit: int = 100, max_attempts: int = 8,
                             initial_retry_seconds: int = 30, max_retry_seconds: int = 3600) -> dict:
    return _sync_credit_applications(session, adapter, limit=limit, max_attempts=max_attempts,
        initial_retry_seconds=initial_retry_seconds, max_retry_seconds=max_retry_seconds)


@app.post("/admin/credit-applications/{sync_id}/retry")
def retry_credit_application_sync(
    sync_id: str, session: Session = Depends(get_session), admin: Principal = Depends(require_admin),
):
    item = session.get(CreditApplicationOutboxRecord, sync_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Credit application sync item not found")
    prior_attempt_count = item.attempt_count
    try:
        retry_credit_application(item)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.add(AuditEvent(match_id=session.get(CreditNoteApplicationRecord, item.application_id).credit_match_id,
        event_type="CREDIT_APPLICATION_SYNC_RETRIED", actor=admin.name,
        details={"sync_id": item.id, "idempotency_key": item.idempotency_key,
                 "previous_attempt_count": prior_attempt_count}))
    session.commit()
    return {"id": item.id, "state": item.state, "next_attempt_at": _timestamp(item.next_attempt_at)}


@app.get("/reviewer/credit-applications")
def reviewer_credit_applications(
    state: str = Query(default="ALL", pattern="^(PENDING|ACKNOWLEDGED|DEAD|ALL)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    query = select(CreditApplicationOutboxRecord, CreditNoteApplicationRecord).join(
        CreditNoteApplicationRecord,
        CreditNoteApplicationRecord.id == CreditApplicationOutboxRecord.application_id)
    if state != "ALL":
        query = query.where(CreditApplicationOutboxRecord.state == state)
    rows = session.execute(query.order_by(CreditApplicationOutboxRecord.created_at.desc()).limit(limit)).all()
    result = []
    for item, application in rows:
        credit, invoice = session.get(MatchRecord, application.credit_match_id), session.get(
            MatchRecord, application.invoice_match_id)
        result.append({"id": item.id, "state": item.state, "attempt_count": item.attempt_count,
            "last_error": item.last_error, "next_attempt_at": _timestamp(item.next_attempt_at),
            "external_reference": item.external_reference, "amount": str(item.amount),
            "currency": (session.get(APOutboxRecord, application.credit_match_id).payload.get("currency")
                         if session.get(APOutboxRecord, application.credit_match_id) else "USD"),
            "credit_match_id": application.credit_match_id, "credit_note_number": credit.invoice_number,
            "invoice_match_id": application.invoice_match_id, "invoice_number": invoice.invoice_number,
            "approved_by": application.approved_by, "created_at": _timestamp(item.created_at),
            "acknowledged_at": _timestamp(item.acknowledged_at)})
    return result


@app.get("/reviewer/payments")
def reviewer_payments(
    status: str = Query(default="ALL", pattern="^(NEEDS_REVIEW|MATCHED|PARTIALLY_MATCHED|ALL)$"),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    query = select(RemittancePaymentRecord).order_by(RemittancePaymentRecord.created_at.desc()).limit(limit)
    if status != "ALL":
        query = query.where(RemittancePaymentRecord.status == status)
    return [payment_result(session, payment) for payment in session.scalars(query).all()]


@app.post("/reviewer/payments/{payment_id}/allocate")
def allocate_payment(
    payment_id: str, body: ManualPaymentAllocationRequest,
    session: Session = Depends(get_session), reviewer: Principal = Depends(require_approver),
):
    payment = session.get(RemittancePaymentRecord, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    try:
        manually_allocate(session, payment, [(item.match_id, item.amount) for item in body.allocations],
                          actor=reviewer.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return payment_result(session, payment)


@app.get("/reviewer/payments/{payment_id}/events")
def payment_events(
    payment_id: str, session: Session = Depends(get_session), reviewer: Principal = Depends(require_reviewer),
):
    if session.get(RemittancePaymentRecord, payment_id) is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    events = session.scalars(select(PaymentReconciliationEventRecord).where(
        PaymentReconciliationEventRecord.payment_id == payment_id
    ).order_by(PaymentReconciliationEventRecord.created_at)).all()
    return [{"id": event.id, "event_type": event.event_type, "actor": event.actor,
             "details": event.details, "created_at": _timestamp(event.created_at)} for event in events]


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
