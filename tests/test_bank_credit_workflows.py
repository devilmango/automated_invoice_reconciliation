from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from invoice_match.database import (
    APOutboxRecord,
    AuditEvent,
    CreditApplicationOutboxRecord,
    CreditNoteApplicationRecord,
    MatchRecord,
    SessionLocal,
)


def _matched_document(client, headers, invoice_number, *, total="100.00", document_type="INVOICE",
                      credit_note_for=None, invoice_date="2026-10-10"):
    request = json.loads((Path(__file__).resolve().parents[1] / "examples" / "match-request.json").read_text())
    request["po"]["number"] = f"PO-{invoice_number}"
    request["receipt"]["items"][0]["qty"] = 100
    request["invoice"].update({"number": invoice_number, "total_amount": total,
                               "document_type": document_type, "invoice_date": invoice_date})
    if credit_note_for:
        request["invoice"]["credit_note_for"] = credit_note_for
    result = client.post("/matches", headers={**headers, "Idempotency-Key": f"test-{invoice_number}"},
                         json=request)
    assert result.status_code == 201, result.text
    return result.json()["match_id"]


def _acknowledge(*match_ids):
    with SessionLocal() as session:
        for match_id in match_ids:
            record = session.get(APOutboxRecord, match_id)
            if record is None:
                match = session.get(MatchRecord, match_id)
                record = APOutboxRecord(match_id=match_id, payload={"currency": "USD",
                    "total_amount": str(match.input_data["invoice"].get("total_amount") or "1000")},
                    state="ACKNOWLEDGED", external_reference=f"external-{match_id}")
                session.add(record)
            record.state = "ACKNOWLEDGED"
            record.external_reference = f"external-{match_id}"
        session.commit()


def test_credit_application_is_queued_and_exposes_retry_history(client, integration_headers,
                                                                 reviewer_headers, monkeypatch):
    from invoice_match import api

    invoice_id = _matched_document(client, integration_headers, "INV-SYNC-1")
    credit_id = _matched_document(client, integration_headers, "CN-SYNC-1",
                                  document_type="CREDIT_NOTE", credit_note_for="INV-SYNC-1")
    _acknowledge(invoice_id, credit_id)
    with SessionLocal() as session:
        session.get(MatchRecord, credit_id).approval_status = "APPROVED"
        session.commit()
    headers = {**reviewer_headers, "Idempotency-Key": "apply-credit-once"}
    response = client.post(f"/reviewer/credit-notes/{credit_id}/apply", headers=headers,
                           json={"invoice_match_id": invoice_id, "amount": "4.00"})
    assert response.status_code == 200, response.text
    assert response.json()["sync_state"] == "PENDING"
    replay = client.post(f"/reviewer/credit-notes/{credit_id}/apply", headers=headers,
                         json={"invoice_match_id": invoice_id, "amount": "4.00"})
    assert replay.json()["replayed"] is True
    with SessionLocal() as session:
        assert len(session.query(CreditApplicationOutboxRecord).all()) == 1

    class FailingAdapter:
        name = "xero"

        def apply_credit_note(self, credit_reference, invoice_reference, amount, *, idempotency_key):
            assert credit_reference == f"external-{credit_id}"
            assert invoice_reference
            assert amount == Decimal("4.00")
            assert idempotency_key
            raise RuntimeError("provider unavailable")

    with SessionLocal() as session:
        session.info["company_id"] = "default"
        result = api.sync_credit_applications(session, FailingAdapter(), max_attempts=3,
                                              initial_retry_seconds=1)
        assert result == {"synced": 0, "failed": 1, "dead_lettered": 0}
    history = client.get(f"/reviewer/credit-notes/{credit_id}/applications", headers=reviewer_headers)
    item = history.json()[0]
    assert item["sync_state"] == "PENDING"
    assert item["attempt_count"] == 1
    assert item["last_error"] == "provider unavailable"
    assert item["next_attempt_at"]
    with SessionLocal() as session:
        assert session.query(AuditEvent).filter_by(event_type="CREDIT_APPLICATION_SYNC_ATTEMPTED").count() == 1


def test_bank_identity_is_account_scoped_and_statement_balances_are_saved(
        client, integration_headers, reviewer_headers):
    content = "transaction_id,date,amount,vendor,reference,currency\n"
    first = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "STMT-1", "account_id": "OPERATING-1", "format": "csv",
        "opening_balance": "1000.00", "closing_balance": "900.00", "content":
        content + "REUSED-ID,2026-10-12,-100.00,ABC Supplies,INV-1,USD\n"})
    second = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "STMT-2", "account_id": "OPERATING-2", "format": "csv",
        "opening_balance": "2000.00", "closing_balance": "1900.00", "content":
        content + "REUSED-ID,2026-10-12,-100.00,ABC Supplies,INV-1,USD\n"})
    assert first.status_code == second.status_code == 201
    assert first.json()["imported"] == second.json()["imported"] == 1
    queue = client.get("/reviewer/bank-transactions", headers=reviewer_headers).json()
    assert len(queue) == 2
    assert {row["bank_account_id"] for row in queue} == {"OPERATING-1", "OPERATING-2"}
    assert {row["external_id"] for row in queue} == {
        "bank:OPERATING-1:REUSED-ID", "bank:OPERATING-2:REUSED-ID"}
    batches = client.get("/reviewer/bank-statements", headers=reviewer_headers).json()
    assert {Decimal(row["closing_balance"]) for row in batches} == {Decimal("900"), Decimal("1900")}


def test_admin_bai2_transaction_code_mapping_controls_direction(client, admin_headers, integration_headers):
    configured = client.put("/admin/bank-accounts/OPERATING-BAI/bai2-codes", headers=admin_headers,
                            json={"transaction_codes": {"495": "CREDIT", "100": "DEBIT"}})
    assert configured.status_code == 200, configured.text
    imported = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "BAI-CODE-MAP", "account_id": "OPERATING-BAI", "format": "bai2",
        "content": "02,test,123456,1,261012,1200,,/\n03,001234,USD,010,0,,,/\n"
                   "16,495,12500,0,BANK-CODE-1,INV-1,ABC Supplies/\n"})
    assert imported.status_code == 201, imported.text
    assert imported.json()["imported"] == 0
    assert imported.json()["non_debit_skipped"] == 1


def test_guided_bank_reconciliation_audits_confirm_split_and_reject(
        client, integration_headers, reviewer_headers, viewer_headers):
    invoice_a = _matched_document(client, integration_headers, "INV-BANK-A")
    invoice_b = _matched_document(client, integration_headers, "INV-BANK-B", invoice_date="2026-09-01")
    _acknowledge(invoice_a, invoice_b)
    imported = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "GUIDED-1", "account_id": "OPERATING-1", "format": "csv",
        "content": "transaction_id,date,amount,vendor,reference,currency\n"
                   "GUIDED-TX,2026-10-12,-200.00,ABC Supplies,INV-BANK-A,USD\n"})
    payment_id = imported.json()["payments"][0]["id"]
    suggestions = client.get(f"/reviewer/bank-transactions/{payment_id}/candidates",
                             headers=reviewer_headers)
    assert suggestions.status_code == 200
    candidates = suggestions.json()["candidates"]
    candidate = next(row for row in candidates if row["match_id"] == invoice_a)
    assert {reason["type"] for reason in candidate["reasons"]} >= {"reference", "vendor", "date"}

    decided = client.post(f"/reviewer/bank-transactions/{payment_id}/decision", headers=viewer_headers,
                          json={"action": "split", "allocations": [
                              {"match_id": invoice_a, "amount": "100"},
                              {"match_id": invoice_b, "amount": "100"}]})
    assert decided.status_code == 403
    approver = {"Authorization": "Bearer test-reviewer-secret-token-for-unit-tests-0002"}
    decided = client.post(f"/reviewer/bank-transactions/{payment_id}/decision", headers=approver,
                          json={"action": "split", "allocations": [
                              {"match_id": invoice_a, "amount": "100"},
                              {"match_id": invoice_b, "amount": "100"}]})
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "MATCHED"
    events = client.get(f"/reviewer/payments/{payment_id}/events", headers=reviewer_headers)
    assert any(event["event_type"] == "BANK_MATCH_SPLIT_CONFIRMED" for event in events.json())

    advice = client.post("/integrations/payments/remittances", headers=integration_headers, json={
        "external_id": "ADVICE-100", "vendor": "ABC Supplies", "currency": "USD",
        "amount": "100.00", "reference": "ADVICE-REF", "payment_date": "2026-10-12T00:00:00Z"})
    bank_with_advice = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "GUIDED-LINK", "account_id": "OPERATING-1", "format": "csv",
        "content": "transaction_id,date,amount,vendor,reference,currency\n"
                   "LINK-TX,2026-10-12,-100.00,ABC Supplies,ADVICE-REF,USD\n"})
    linked_bank_id = bank_with_advice.json()["payments"][0]["id"]
    candidates = client.get(f"/reviewer/bank-transactions/{linked_bank_id}/candidates",
                           headers=reviewer_headers).json()
    assert candidates["payment_candidates"][0]["payment_id"] == advice.json()["id"]
    linked = client.post(f"/reviewer/bank-transactions/{linked_bank_id}/decision", headers=approver,
        json={"action": "link_payment", "payment_match_id": advice.json()["id"], "allocations": []})
    assert linked.status_code == 200 and linked.json()["matched_remittance_id"] == advice.json()["id"]

    invoice_c = _matched_document(client, integration_headers, "INV-BANK-C", invoice_date="2026-08-01")
    _acknowledge(invoice_c)
    confirmed = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "GUIDED-CONFIRM", "account_id": "OPERATING-1", "format": "csv",
        "content": "transaction_id,date,amount,vendor,reference,currency\n"
                   "CONFIRM-TX,2026-10-12,-100.00,ABC Supplies,INV-BANK-C,USD\n"})
    confirmed_id = confirmed.json()["payments"][0]["id"]
    result = client.post(f"/reviewer/bank-transactions/{confirmed_id}/decision", headers=approver,
        json={"action": "confirm", "allocations": [{"match_id": invoice_c, "amount": "100"}]})
    assert result.status_code == 200 and result.json()["status"] == "MATCHED"

    rejected = client.post("/integrations/bank-statements/import", headers=integration_headers, json={
        "statement_id": "GUIDED-REJECT", "account_id": "OPERATING-1", "format": "csv",
        "content": "transaction_id,date,amount,vendor,reference,currency\n"
                   "REJECT-TX,2026-10-12,-1.00,ABC Supplies,UNKNOWN,USD\n"})
    rejected_id = rejected.json()["payments"][0]["id"]
    result = client.post(f"/reviewer/bank-transactions/{rejected_id}/decision", headers=approver,
        json={"action": "reject", "allocations": []})
    assert result.status_code == 200 and result.json()["status"] == "NEEDS_REVIEW"
    events = client.get(f"/reviewer/payments/{rejected_id}/events", headers=reviewer_headers)
    assert any(event["event_type"] == "BANK_MATCH_REJECTED" for event in events.json())
