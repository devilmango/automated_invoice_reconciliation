from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

from invoice_match.matcher import match_documents
from invoice_match.rules import ToleranceRules, load_rules
from invoice_match.schemas import MatchRequest, MatchStatus

ROOT = Path(__file__).resolve().parents[1]


def sample_request() -> dict:
    return json.loads((ROOT / "examples" / "match-request.json").read_text())


def evaluate(payload: dict, rules: ToleranceRules | None = None):
    return match_documents(
        MatchRequest.model_validate(payload), rules or load_rules()
    )


def test_invoice_quantity_over_receipt_creates_expected_exception():
    result = evaluate(sample_request())

    assert result.status == MatchStatus.EXCEPTION
    assert result.reason == "RECEIPT_QUANTITY_MISMATCH"
    assert result.expected == Decimal("95")
    assert result.invoiced == Decimal("100")
    assert result.variance == Decimal("5")


def test_partial_receipt_and_matching_partial_invoice_are_accepted():
    payload = sample_request()
    payload["invoice"]["items"][0]["qty"] = 95

    result = evaluate(payload)

    assert result.status == MatchStatus.MATCHED
    assert result.discrepancies == []
    assert result.approval_status == "NOT_REQUIRED"


def test_quantity_variance_at_tolerance_boundary_is_accepted():
    payload = sample_request()
    payload["receipt"]["items"][0]["qty"] = 100
    payload["invoice"]["items"][0]["qty"] = 102

    result = evaluate(payload)

    assert result.status == MatchStatus.MATCHED


def test_price_variance_over_tolerance_is_reported():
    payload = sample_request()
    payload["receipt"]["items"][0]["qty"] = 100
    payload["invoice"]["items"][0]["price"] = "10.11"

    result = evaluate(payload)

    assert result.reason == "PRICE_MISMATCH"
    assert result.expected == Decimal("10")
    assert result.invoiced == Decimal("10.11")
    assert result.variance == Decimal("0.11")


def test_price_variance_at_tolerance_boundary_is_accepted():
    payload = sample_request()
    payload["receipt"]["items"][0]["qty"] = 100
    payload["invoice"]["items"][0]["price"] = "10.10"

    result = evaluate(payload)

    assert result.status == MatchStatus.MATCHED


def test_prices_in_different_currencies_compare_in_base_currency():
    payload = sample_request()
    payload["receipt"]["items"][0]["qty"] = 100
    payload["po"]["currency"] = "EUR"
    payload["po"]["items"][0]["price"] = 10
    payload["invoice"]["currency"] = "USD"
    payload["invoice"]["items"][0]["price"] = 10.8
    rules = ToleranceRules(
        base_currency="USD",
        rates_to_base={"USD": Decimal("1"), "EUR": Decimal("1.08")},
    )

    result = evaluate(payload, rules)

    assert result.status == MatchStatus.MATCHED
    assert all(issue.reason != "PRICE_MISMATCH" for issue in result.discrepancies)


def test_currency_without_configured_conversion_rate_is_an_exception():
    payload = sample_request()
    payload["po"]["currency"] = "EUR"
    payload["receipt"]["items"][0]["qty"] = 100

    result = evaluate(payload)

    assert result.status == MatchStatus.EXCEPTION
    assert result.reason == "CURRENCY_MISMATCH"


def test_invoice_vendor_must_match_purchase_order_vendor():
    payload = sample_request()
    payload["invoice"]["vendor"] = "Different Supplier"

    result = evaluate(payload)

    assert result.reason == "VENDOR_MISMATCH"
    assert result.expected == "ABC Supplies"
    assert result.invoiced == "Different Supplier"


def test_tax_variance_is_checked_when_both_documents_supply_tax():
    payload = sample_request()
    payload["receipt"]["items"][0]["qty"] = 100
    payload["po"]["tax_rate"] = "0.1"
    payload["invoice"]["tax_amount"] = "101"

    result = evaluate(payload)

    tax_issue = next(issue for issue in result.discrepancies if issue.reason == "TAX_MISMATCH")
    assert tax_issue.expected == Decimal("100.0")
    assert tax_issue.invoiced == Decimal("101")


def test_duplicate_sku_rows_use_weighted_average_unit_price():
    payload = copy.deepcopy(sample_request())
    payload["po"]["items"] = [
        {"sku": "A100", "qty": 1, "price": 10},
        {"sku": "A100", "qty": 1, "price": 12},
    ]
    payload["receipt"]["items"] = [{"sku": "A100", "qty": 2}]
    payload["invoice"]["items"] = [{"sku": "A100", "qty": 2, "price": 11}]

    result = evaluate(payload)

    assert result.status == MatchStatus.MATCHED
