from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .database import ProcurementRecord, ProcurementSyncState
from .schemas import PurchaseOrder, ReceiptDocument


def sync_procurement(session: Session, adapter) -> dict:
    """Fetch the provider's current PO/receipt snapshot and persist valid records."""
    now = datetime.now(timezone.utc)
    company_id = session.info.get("company_id", "default")
    state = session.get(ProcurementSyncState, (company_id, adapter.name))
    if state is None:
        state = ProcurementSyncState(source_adapter=adapter.name)
        session.add(state)
    checkpoint = state.last_successful_at
    if checkpoint is not None and checkpoint.tzinfo is None:
        checkpoint = checkpoint.replace(tzinfo=timezone.utc)
    summary = {
        "adapter": adapter.name,
        "updated_since": checkpoint.isoformat() if checkpoint else None,
        "discovered": 0,
        "synced": 0,
        "failed": 0,
        "errors": [],
    }
    try:
        purchase_orders = adapter.list_purchase_orders(updated_since=checkpoint)
        if not isinstance(purchase_orders, list):
            raise ValueError("Procurement adapter must return a list of purchase orders")
    except Exception as exc:
        summary["failed"] = 1
        summary["errors"] = [{"purchase_order": None, "error": str(exc)[:500]}]
        state.last_run_at = now
        state.last_run_summary = summary
        session.commit()
        return {**summary, "last_successful_at": checkpoint.isoformat() if checkpoint else None}

    summary["discovered"] = len(purchase_orders)
    for raw_order in purchase_orders:
        number = None
        try:
            with session.begin_nested():
                source = raw_order.get("purchase_order", raw_order)
                purchase_order = PurchaseOrder.model_validate(source)
                number = purchase_order.number
                if not number:
                    raise ValueError("Purchase order number is required")
                raw_receipts = adapter.fetch_receipts(number)
                normalized_receipts = [
                    ReceiptDocument.model_validate(receipt).model_dump(mode="json")
                    for receipt in raw_receipts
                ]
                record = session.get(ProcurementRecord, (company_id, number))
                if record is None:
                    record = ProcurementRecord(po_number=number)
                    session.add(record)
                record.purchase_order = purchase_order.model_dump(mode="json")
                record.receipts = normalized_receipts
                record.source_adapter = adapter.name
                record.synced_at = datetime.now(timezone.utc)
                session.flush()
            summary["synced"] += 1
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append({
                "purchase_order": number,
                "error": str(exc)[:500],
            })
    session.commit()
    state.last_run_at = now
    state.last_run_summary = dict(summary)
    if summary["failed"] == 0:
        state.last_successful_at = now
    session.commit()
    return {
        **summary,
        "last_successful_at": state.last_successful_at.isoformat() if state.last_successful_at else None,
    }
