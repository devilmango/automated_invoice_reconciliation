from __future__ import annotations

from decimal import Decimal
from itertools import combinations
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import (APOutboxRecord, AuditEvent, MatchRecord, PaymentAllocationRecord,
                      PaymentReconciliationEventRecord, RemittancePaymentRecord)


def _vendor(value: str) -> str:
    return " ".join(value.casefold().split())


def _outstanding(session: Session, outbox: APOutboxRecord) -> Decimal:
    allocated = session.scalar(select(func.sum(PaymentAllocationRecord.amount)).where(
        PaymentAllocationRecord.match_id == outbox.match_id
    ))
    return max(Decimal(str(outbox.payload.get("total_amount") or 0)) - Decimal(str(allocated or 0)), Decimal(0))


def _candidates(session: Session, payment: RemittancePaymentRecord):
    rows = session.execute(select(APOutboxRecord, MatchRecord).join(
        MatchRecord, MatchRecord.id == APOutboxRecord.match_id
    ).where(APOutboxRecord.state == "ACKNOWLEDGED")).all()
    found = []
    for outbox, match in rows:
        if _vendor(match.vendor) != _vendor(payment.vendor):
            continue
        if str(outbox.payload.get("currency") or "USD").upper() != payment.currency:
            continue
        if outbox.accounting_status in {"PAID", "VOIDED"}:
            continue
        remaining = _outstanding(session, outbox)
        if remaining > 0:
            found.append((outbox, match, remaining))
    return found


def _add_allocations(session: Session, payment: RemittancePaymentRecord, allocations: list[tuple[APOutboxRecord, Decimal]]) -> None:
    for outbox, amount in allocations:
        allocation = session.scalar(select(PaymentAllocationRecord).where(
            PaymentAllocationRecord.payment_id == payment.id,
            PaymentAllocationRecord.match_id == outbox.match_id,
        ))
        if allocation is None:
            allocation = PaymentAllocationRecord(id=str(uuid4()), payment_id=payment.id,
                                                 match_id=outbox.match_id, amount=amount)
            session.add(allocation)
        else:
            allocation.amount = Decimal(str(allocation.amount)) + amount
        session.add(AuditEvent(match_id=outbox.match_id, event_type="PAYMENT_ALLOCATED",
                               actor="payment-reconciliation", details={
                                   "payment_id": payment.id, "amount": str(amount),
                                   "currency": payment.currency,
                               }))
    payment.allocated_amount = Decimal(str(payment.allocated_amount or 0)) + sum(
        (amount for _, amount in allocations), Decimal(0)
    )
    payment.status = "MATCHED" if payment.allocated_amount == payment.amount else "PARTIALLY_MATCHED"


def reconcile_payment(session: Session, payment: RemittancePaymentRecord, *, actor: str) -> None:
    candidates = _candidates(session, payment)
    assignments: list[tuple[APOutboxRecord, Decimal]] = []
    requested = payment.input_data.get("allocations")
    if requested:
        for advice in requested:
            reference = str(advice["invoice_reference"]).strip().casefold()
            matches = [(outbox, match, remaining) for outbox, match, remaining in candidates
                       if reference in {outbox.match_id.casefold(), str(outbox.external_reference or "").casefold(),
                                        str(match.invoice_number or "").casefold()}]
            if len(matches) != 1 or Decimal(str(advice["amount"])) > matches[0][2]:
                assignments = []
                break
            assignments.append((matches[0][0], Decimal(str(advice["amount"]))))
        if assignments and sum((amount for _, amount in assignments), Decimal(0)) == payment.amount:
            _add_allocations(session, payment, assignments)
        else:
            payment.status = "NEEDS_REVIEW"
    else:
        reference = str(payment.reference or "").strip().casefold()
        referenced = [(outbox, remaining) for outbox, match, remaining in candidates
                      if reference and reference in {outbox.match_id.casefold(),
                          str(outbox.external_reference or "").casefold(),
                          str(match.invoice_number or "").casefold()}]
        if len(referenced) == 1 and payment.amount <= referenced[0][1]:
            assignments = [(referenced[0][0], payment.amount)]
        elif len(referenced) > 1:
            assignments = []
        exact = [(outbox, remaining) for outbox, _, remaining in candidates if remaining == payment.amount]
        if not assignments and len(referenced) == 0 and len(exact) == 1:
            assignments = exact
        elif not assignments and len(referenced) == 0 and len(exact) == 0:
            partial = [(outbox, remaining) for outbox, _, remaining in candidates if remaining > payment.amount]
            # A short payment may be applied automatically only when it points to one invoice.
            if len(partial) == 1:
                assignments = [(partial[0][0], payment.amount)]
            else:
                # Match a remittance covering multiple invoices only when its full amount
                # identifies one and only one combination.
                solutions = []
                for count in range(2, min(len(candidates), 8) + 1):
                    solutions.extend(combo for combo in combinations(candidates, count)
                                     if sum((row[2] for row in combo), Decimal(0)) == payment.amount)
                    if len(solutions) > 1:
                        break
                if len(solutions) == 1:
                    assignments = [(row[0], row[2]) for row in solutions[0]]
        if assignments:
            _add_allocations(session, payment, assignments)
        else:
            payment.status = "NEEDS_REVIEW"
    session.add(PaymentReconciliationEventRecord(
        payment_id=payment.id, event_type="PAYMENT_RECONCILED", actor=actor,
        details={"status": payment.status, "allocated_amount": str(payment.allocated_amount),
                 "allocation_count": len(assignments)},
    ))


def payment_result(session: Session, payment: RemittancePaymentRecord) -> dict:
    allocations = session.execute(select(PaymentAllocationRecord, MatchRecord).join(
        MatchRecord, MatchRecord.id == PaymentAllocationRecord.match_id
    ).where(PaymentAllocationRecord.payment_id == payment.id)).all()
    return {
        "id": payment.id, "external_id": payment.external_id, "vendor": payment.vendor,
        "currency": payment.currency, "amount": payment.amount, "reference": payment.reference,
        "payment_date": payment.payment_date, "status": payment.status,
        "allocated_amount": payment.allocated_amount,
        "unallocated_amount": payment.amount - payment.allocated_amount,
        "created_at": payment.created_at,
        "allocations": [{"match_id": allocation.match_id, "invoice_number": match.invoice_number,
                         "amount": allocation.amount} for allocation, match in allocations],
    }


def manually_allocate(session: Session, payment: RemittancePaymentRecord,
                      requested: list[tuple[str, Decimal]], *, actor: str) -> None:
    candidates = {outbox.match_id: (outbox, remaining) for outbox, _, remaining in _candidates(session, payment)}
    total = sum((amount for _, amount in requested), Decimal(0))
    if Decimal(str(payment.allocated_amount or 0)) + total > payment.amount:
        raise ValueError("Allocations cannot exceed payment amount")
    for match_id, amount in requested:
        if match_id not in candidates:
            raise ValueError(f"Invoice {match_id} is not an outstanding acknowledged invoice for this vendor and currency")
        if amount > candidates[match_id][1]:
            raise ValueError(f"Allocation exceeds the outstanding invoice balance for {match_id}")
    _add_allocations(session, payment, [(candidates[key][0], amount) for key, amount in requested])
    session.add(PaymentReconciliationEventRecord(
        payment_id=payment.id, event_type="PAYMENT_MANUALLY_ALLOCATED", actor=actor,
        details={"status": payment.status, "allocated_amount": str(payment.allocated_amount),
                 "allocation_count": len(requested)},
    ))
