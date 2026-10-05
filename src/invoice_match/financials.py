from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .rules import ToleranceRules
from .schemas import MatchRequest


def round_currency(value: Decimal, currency: str, rules: ToleranceRules) -> Decimal:
    minor_units = rules.minor_units.get(currency.upper(), 2)
    quantum = Decimal(1).scaleb(-minor_units)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def to_base_currency(value: Decimal, currency: str, rules: ToleranceRules) -> Decimal | None:
    rate = rules.rates_to_base.get(currency.upper())
    if rate is None:
        return None
    return round_currency(value * rate, rules.base_currency, rules)


def line_net_amount(item) -> Decimal:
    if item.price is None:
        return Decimal(0)
    return item.qty * item.price * (Decimal(1) - item.discount_rate)


def item_tax_rate(item, po_item, po_document_rate: Decimal | None) -> Decimal | None:
    if po_item and po_item["tax_rate"] is not None:
        return po_item["tax_rate"]
    if item.tax_rate is not None:
        return item.tax_rate
    return po_document_rate


def invoice_summary(request: MatchRequest, rules: ToleranceRules) -> dict[str, Decimal | None]:
    invoice = request.invoice
    po_rates: dict[str, set[Decimal | None]] = {}
    for item in request.po.items:
        rate = item.tax_rate if item.tax_rate is not None else request.po.tax_rate
        po_rates.setdefault(item.sku, set()).add(rate)
    po_by_sku = {
        sku: next(iter(rates)) if len(rates) == 1 and None not in rates else None
        for sku, rates in po_rates.items()
    }

    sign = Decimal(-1) if invoice.document_type == "CREDIT_NOTE" else Decimal(1)
    subtotal = round_currency(sum((line_net_amount(item) for item in invoice.items), Decimal(0)), invoice.currency, rules)
    if invoice.tax_amount is not None:
        tax = round_currency(invoice.tax_amount, invoice.currency, rules)
    else:
        expected_parts: list[Decimal] = []
        all_tax_rates_known = True
        for item in invoice.items:
            if item.tax_amount is not None:
                expected_parts.append(item.tax_amount)
                continue
            rate = po_by_sku.get(item.sku)
            if rate is None:
                rate = item.tax_rate
            if rate is None:
                all_tax_rates_known = False
                break
            expected_parts.append(line_net_amount(item) * rate)
        tax = (
            round_currency(sum(expected_parts, Decimal(0)), invoice.currency, rules)
            if all_tax_rates_known and expected_parts
            else round_currency(sum((item.tax_amount or Decimal(0) for item in invoice.items), Decimal(0)), invoice.currency, rules)
        )
    freight = round_currency(invoice.freight_amount, invoice.currency, rules)
    discount = round_currency(invoice.discount_amount, invoice.currency, rules)
    subtotal *= sign
    tax *= sign
    freight *= sign
    discount *= -sign
    calculated_total = round_currency(subtotal + freight + tax - discount, invoice.currency, rules)
    total = (
        round_currency(invoice.total_amount, invoice.currency, rules) * sign
        if invoice.total_amount is not None else calculated_total
    )
    return {
        "subtotal": subtotal,
        "tax": tax,
        "freight": freight,
        "discount": discount,
        "calculated_total": calculated_total,
        "total": total,
    }
