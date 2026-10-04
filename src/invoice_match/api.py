from __future__ import annotations

from datetime import timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import AuditEvent, ExceptionRecord, MatchRecord, SessionLocal
from .matcher import match_documents
from .rules import load_rules
from .reviewer_auth import require_reviewer
from .schemas import ApprovalDecision, AuditEntry, ExceptionQueueItem, MatchRequest, MatchResult, MatchStatus


app = FastAPI(
    title="invoice-match",
    description="A lightweight, self-hosted three-way invoice matching engine for SMB finance teams.",
    version="0.1.0",
)


def get_session():
    with SessionLocal() as session:
        yield session


def _timestamp(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/matches", response_model=MatchResult, status_code=201)
def create_match(
    request: MatchRequest,
    session: Session = Depends(get_session),
    actor: str = Header(default="api", alias="X-Actor"),
):
    rules = load_rules()
    result = match_documents(request, rules)
    match_id = str(uuid4())
    result.match_id = match_id
    record = MatchRecord(
        id=match_id,
        status=result.status.value,
        approval_status=result.approval_status,
        po_number=request.po.number,
        invoice_number=request.invoice.number,
        vendor=request.po.vendor,
        reason=result.reason,
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
    session.add(AuditEvent(
        match_id=match_id,
        event_type="MATCH_CREATED",
        actor=actor,
        details={"status": result.status.value, "reason": result.reason},
    ))
    session.commit()
    return result


@app.get("/matches/{match_id}", response_model=MatchResult)
def get_match(
    match_id: str,
    session: Session = Depends(get_session),
    reviewer: str = Depends(require_reviewer),
):
    record = session.get(MatchRecord, match_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return record.result_data


@app.get("/exceptions", response_model=list[ExceptionQueueItem])
def list_exceptions(
    status: str = Query(default="OPEN", pattern="^(OPEN|CLOSED|ALL)$"),
    session: Session = Depends(get_session),
    reviewer: str = Depends(require_reviewer),
):
    query = select(ExceptionRecord, MatchRecord).join(MatchRecord, ExceptionRecord.match_id == MatchRecord.id)
    if status != "ALL":
        query = query.where(ExceptionRecord.queue_status == status)
    query = query.order_by(ExceptionRecord.created_at.asc())
    rows = session.execute(query).all()
    return [ExceptionQueueItem(
        match_id=match.id,
        status=MatchStatus(match.status),
        approval_status=match.approval_status,
        po_number=match.po_number,
        invoice_number=match.invoice_number,
        vendor=match.vendor,
        reason=match.reason or "UNKNOWN",
        created_at=_timestamp(exception.created_at),
    ) for exception, match in rows]


def _decide(
    match_id: str,
    decision: ApprovalDecision,
    outcome: str,
    reviewer: str,
    session: Session,
):
    record = session.get(MatchRecord, match_id)
    exception = session.scalar(select(ExceptionRecord).where(ExceptionRecord.match_id == match_id))
    if record is None or exception is None:
        raise HTTPException(status_code=404, detail="Exception not found")
    if record.approval_status != "PENDING" or exception.queue_status != "OPEN":
        raise HTTPException(status_code=409, detail="This exception has already been decided")
    record.approval_status = outcome
    exception.queue_status = "CLOSED"
    result = dict(record.result_data)
    result["approval_status"] = outcome
    record.result_data = result
    session.add(AuditEvent(
        match_id=match_id,
        event_type=f"EXCEPTION_{outcome}",
        actor=reviewer,
        details={"comment": decision.comment},
    ))
    session.commit()
    return result


@app.post("/exceptions/{match_id}/approve", response_model=MatchResult)
def approve_exception(
    match_id: str,
    decision: ApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: str = Depends(require_reviewer),
):
    return _decide(match_id, decision, "APPROVED", reviewer, session)


@app.post("/exceptions/{match_id}/reject", response_model=MatchResult)
def reject_exception(
    match_id: str,
    decision: ApprovalDecision,
    session: Session = Depends(get_session),
    reviewer: str = Depends(require_reviewer),
):
    return _decide(match_id, decision, "REJECTED", reviewer, session)


@app.get("/matches/{match_id}/audit", response_model=list[AuditEntry])
def match_audit(
    match_id: str,
    session: Session = Depends(get_session),
    reviewer: str = Depends(require_reviewer),
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
