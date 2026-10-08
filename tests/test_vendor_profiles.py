from __future__ import annotations

from invoice_match.document_capture import extract_document
from invoice_match.vendor_profiles import vendors_equivalent


def test_vendor_profile_applies_alias_labels_currency_and_sku_mapping(monkeypatch, tmp_path):
    profiles = tmp_path / "vendor-profiles.yaml"
    profiles.write_text("""vendors:
  - name: Acme Wholesale
    aliases: [Acme Co]
    labels:
      vendor: [Supplier name]
      number: [Invoice Ref]
      invoice_date: [Billing date]
      total_amount: [Balance payable]
    date_format: '%d/%m/%Y'
    default_currency: EUR
    sku_aliases:
      ACME-1: SKU-1
""", encoding="utf-8")
    monkeypatch.setenv("INVOICE_MATCH_VENDOR_PROFILES", str(profiles))

    fields, confidence, notes, _digest, _content_type = extract_document(
        b"Supplier name: Acme Co\nInvoice Ref: AC-204\nBilling date: 06/10/2026\n"
        b"Balance payable: 12.50\nITEM: ACME-1 | 1 | 12.50",
        "invoice.txt",
        "text/plain",
    )

    assert fields["vendor"] == "Acme Wholesale"
    assert fields["number"] == "AC-204"
    assert fields["invoice_date"] == "2026-10-06"
    assert fields["currency"] == "EUR"
    assert fields["items"][0]["sku"] == "SKU-1"
    assert confidence["currency"] == 0.90
    assert "VENDOR_PROFILE_APPLIED:Acme Wholesale" in notes
    assert vendors_equivalent("Acme Co", "Acme Wholesale")


def test_capture_inbox_page_is_available_and_documents_remain_reviewer_protected(
    client, reviewer_headers, integration_headers
):
    page = client.get("/reviewer/captures")
    unauthenticated = client.get("/documents")

    assert page.status_code == 200
    assert "Original invoice PDF preview" in page.text
    assert "Verify invoice" in page.text
    assert unauthenticated.status_code == 401
    assert client.get("/documents", headers=reviewer_headers).status_code == 200
