from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from invoice_match.bank_statement import parse_statement
from invoice_match.database import APOutboxRecord, AccountingConnectionRecord, MatchRecord, SessionLocal
from invoice_match.oidc_auth import _sign, cookie_name


def test_bank_statement_parsers_normalize_csv_ofx_and_bai2():
    csv_rows = parse_statement("csv", "transaction_id,date,debit,credit,payee,currency\n"
        "TX-1,2026-10-12,125.00,,ABC Supplies,USD\n")
    assert csv_rows[0]["external_id"] == "TX-1"
    assert csv_rows[0]["direction"] == "DEBIT"
    assert csv_rows[0]["amount"] == Decimal("125.00")
    ofx_rows = parse_statement("ofx", "<OFX><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT"
        "<DTPOSTED>20261012120000<TRNAMT>-10.25<FITID>OFX-1<NAME>ABC</STMTTRN>"
        "</BANKTRANLIST></OFX>")
    assert ofx_rows[0]["external_id"] == "OFX-1"
    assert ofx_rows[0]["amount"] == Decimal("10.25")
    bai_rows = parse_statement("bai2", "02,test,123456,1,261012,1200,,/\n"
        "03,001234,USD,010,0,,,/\n16,495,12500,0,BANK-1,INV-1,ABC Supplies/\n")
    assert bai_rows[0]["direction"] == "DEBIT"
    assert bai_rows[0]["amount"] == Decimal("125")


def test_bank_import_is_idempotent_and_queues_supplier_payment(client, integration_headers, reviewer_headers):
    body = {"statement_id": "STMT-2026-01", "format": "csv", "content":
        "transaction_id,date,amount,vendor,reference,currency\n"
        "BANK-TX-88,2026-10-12,-25.00,ABC Supplies,INV-88,USD\n"
        "BANK-TX-CREDIT,2026-10-12,25.00,Refund,RF-88,USD\n"}
    first = client.post("/integrations/bank-statements/import", headers=integration_headers, json=body)
    again = client.post("/integrations/bank-statements/import", headers=integration_headers, json=body)
    queue = client.get("/reviewer/bank-transactions", headers=reviewer_headers)
    assert first.status_code == 201
    assert first.json()["imported"] == 1 and first.json()["non_debit_skipped"] == 1
    assert again.json()["imported"] == 0 and again.json()["duplicates"] == 1
    assert len(queue.json()) == 1
    assert queue.json()[0]["external_id"] == "bank:default:BANK-TX-88"
    assert queue.json()[0]["status"] == "NEEDS_REVIEW"


def test_oidc_signed_session_uses_explicit_role_and_company_mapping(client, monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("OIDC_CLIENT_ID", "invoice-match")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app.example.test/auth/oidc/callback")
    monkeypatch.setenv("OIDC_SESSION_SECRET", "this-test-secret-has-at-least-thirty-two-bytes")
    monkeypatch.setenv("OIDC_ROLE_MAP", json.dumps({"finance-reviewers": ["reviewer"]}))
    now = int(time.time())
    token = _sign({"sub": "user-123", "name": "Finance Reviewer", "roles": ["reviewer"],
                   "company_id": "acme", "iat": now, "exp": now + 3600},
                  purpose="reviewer-session")
    client.cookies.set(cookie_name(), token)
    response = client.get("/auth/oidc/me")
    assert response.status_code == 200
    assert response.json() == {"name": "Finance Reviewer", "roles": ["reviewer"], "company_id": "acme"}


def test_oidc_authorization_code_flow_binds_state_and_pkce(monkeypatch):
    from invoice_match import oidc_auth

    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example.test")
    monkeypatch.setenv("OIDC_CLIENT_ID", "invoice-match")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app.example.test/auth/oidc/callback")
    monkeypatch.setenv("OIDC_SESSION_SECRET", "this-test-secret-has-at-least-thirty-two-bytes")
    monkeypatch.setenv("OIDC_ROLE_MAP", json.dumps({"finance-reviewers": ["reviewer"]}))
    discovery = {"issuer": "https://identity.example.test", "authorization_endpoint": "https://identity.example.test/authorize",
                 "token_endpoint": "https://identity.example.test/token", "jwks_uri": "https://identity.example.test/keys"}
    monkeypatch.setattr(oidc_auth, "_discovery", lambda: discovery)
    authorization_url, state_cookie = oidc_auth.begin_login("/reviewer")
    query = parse_qs(urlparse(authorization_url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] and query["nonce"]
    saved = oidc_auth._unsign(state_cookie, purpose="oidc-state", max_age=600)
    exchanged = []
    monkeypatch.setattr(oidc_auth, "_exchange", lambda code, verifier, _discovery, _config:
                        exchanged.append((code, verifier)) or {"id_token": "mock-token"})
    monkeypatch.setattr(oidc_auth, "_verify_id_token", lambda token, nonce, _discovery, _config: {
        "sub": "oidc-user", "groups": ["finance-reviewers"], "company_id": "acme"})
    session_token, return_to = oidc_auth.finish_login("authorization-code", query["state"][0], state_cookie)
    principal = oidc_auth._unsign(session_token, purpose="reviewer-session", max_age=3600)
    assert exchanged == [("authorization-code", saved["verifier"])]
    assert return_to == "/reviewer"
    assert principal["roles"] == ["reviewer"] and principal["company_id"] == "acme"


def test_admin_can_sync_and_review_accounting_master_data(client, admin_headers, monkeypatch):
    from invoice_match import api

    with SessionLocal() as session:
        session.add(AccountingConnectionRecord(id="test-accounting-connection", provider="quickbooks",
            encrypted_config="not-used-by-this-test", enabled=True, updated_by="admin"))
        session.commit()

    class Adapter:
        name = "quickbooks"

        def fetch_master_data(self):
            return {"supplier": [{"source_id": "vendor-1", "name": "ABC Supplies",
                                  "code": "ABC", "active": True, "source_data": {"Id": "vendor-1"}}]}

    monkeypatch.setattr(api, "create_adapter", lambda provider, company_id: Adapter())
    synced = client.post("/admin/accounting-connections/quickbooks/sync-masters", headers=admin_headers)
    listing = client.get("/admin/accounting-connections/quickbooks/master-data?category=supplier",
                         headers=admin_headers)
    assert synced.status_code == 200 and synced.json()["counts"] == {"supplier": 1}
    assert listing.json()[0]["source_id"] == "vendor-1"
    assert listing.json()[0]["name"] == "ABC Supplies"


def test_accounting_status_failures_schedule_backoff_and_are_audited(clear_database):
    from invoice_match.accounting_status import refresh_due_accounting_statuses

    with SessionLocal() as session:
        match = MatchRecord(id="status-match", status="MATCHED", approval_status="APPROVED",
            po_number="PO-1", invoice_number="INV-1", vendor="ABC", vendor_key="abc",
            approval_policy="default", required_role="approver", escalation_role="admin",
            approvals_required=1, approvals_received=1, rule_digest="", rule_snapshot={},
            input_data={"invoice": {"document_type": "INVOICE"}}, result_data={})
        outbox = APOutboxRecord(match_id=match.id, payload={"currency": "USD", "total_amount": "10"},
            state="ACKNOWLEDGED", external_reference="EXT-1")
        session.add_all([match, outbox])
        session.commit()

        class Adapter:
            name = "test"

            def fetch_invoice_status(self, reference):
                error = RuntimeError("provider rate limited request")
                error.status = 429
                error.retry_after = "600"
                raise error

        result = refresh_due_accounting_statuses(session, Adapter(), minimum_interval=0)
        session.refresh(outbox)
        assert result == {"checked": 0, "changed": 0, "failed": 1}
        assert outbox.accounting_status_attempt_count == 1
        assert outbox.accounting_status_error == "provider rate limited request"
        assert outbox.accounting_status_next_retry_at is not None
        retry_at = outbox.accounting_status_next_retry_at.replace(tzinfo=timezone.utc)
        assert retry_at >= datetime.now(timezone.utc) + timedelta(seconds=599)


def test_credit_note_application_requires_link_and_decrements_invoice_balance(client, integration_headers,
                                                                              reviewer_headers):
    from pathlib import Path

    request = json.loads((Path(__file__).resolve().parents[1] / "examples" / "match-request.json").read_text())
    request["receipt"]["items"][0]["qty"] = 100
    original = client.post("/matches", headers={**integration_headers, "Idempotency-Key": "invoice-credit-test-1"},
                           json=request)
    assert original.status_code == 201
    invoice_id = original.json()["match_id"]
    credit_request = json.loads(json.dumps(request))
    credit_request["invoice"].update({"number": "CN-100", "document_type": "CREDIT_NOTE",
                                      "credit_note_for": request["invoice"]["number"], "total_amount": "10.00"})
    credit = client.post("/matches", headers={**integration_headers, "Idempotency-Key": "credit-note-test-1"},
                         json=credit_request)
    assert credit.status_code == 201
    credit_id = credit.json()["match_id"]
    with SessionLocal() as session:
        credit_match = session.get(MatchRecord, credit_id)
        credit_match.approval_status = "APPROVED"
        credit_outbox, invoice_outbox = session.get(APOutboxRecord, credit_id), session.get(APOutboxRecord, invoice_id)
        credit_outbox.state = invoice_outbox.state = "ACKNOWLEDGED"
        session.commit()
    applied = client.post(f"/reviewer/credit-notes/{credit_id}/apply", headers=reviewer_headers,
                          json={"invoice_match_id": invoice_id, "amount": "4.00", "note": "Partial credit"})
    assert applied.status_code == 200
    assert applied.json()["credit_remaining"] == "6.00"
    assert applied.json()["invoice_remaining"] == "996.00"
    events = client.get(f"/reviewer/credit-notes/{credit_id}/applications", headers=reviewer_headers)
    assert len(events.json()) == 1
