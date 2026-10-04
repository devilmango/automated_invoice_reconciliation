from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .rules import ToleranceRules
from .schemas import Discrepancy, MatchRequest, MatchResult, MatchStatus


def _totals(items: list) -> dict[str, dict]:
    grouped: dict[str, dict] = defaultdict(
        lambda: {"qty": Decimal(0), "priced_qty": Decimal(0), "amount": Decimal(0)}
    )
    for item in items:
        grouped[item.sku]["qty"] += item.qty
        if item.price is not None:
            grouped[item.sku]["priced_qty"] += item.qty
            grouped[item.sku]["amount"] += item.qty * item.price
    return {
        sku: {
            "qty": values["qty"],
            "price": values["amount"] / values["priced_qty"] if values["priced_qty"] else None,
        }
        for sku, values in grouped.items()
    }


def _outside_tolerance(expected: Decimal, actual: Decimal, tolerance: Decimal) -> bool:
    variance = abs(actual - expected)
    allowed = abs(expected) * tolerance
    return variance > allowed


def _money(value: Decimal, currency: str, rules: ToleranceRules) -> Decimal | None:
    rate = rules.rates_to_base.get(currency.upper())
    return value * rate if rate is not None else None


def match_documents(request: MatchRequest, rules: ToleranceRules) -> MatchResult:
    po, receipt, invoice = request.po, request.receipt, request.invoice
    discrepancies: list[Discrepancy] = []
    po_currency, invoice_currency = po.currency.upper(), invoice.currency.upper()
    po_rate = rules.rates_to_base.get(po_currency)
    invoice_rate = rules.rates_to_base.get(invoice_currency)

    if po_rate is None or invoice_rate is None:
        discrepancies.append(Discrepancy(
            reason="CURRENCY_MISMATCH", expected=po_currency, invoiced=invoice_currency,
            variance="N/A", message="A currency is missing from rules.currency.rates_to_base.",
        ))
    if invoice.vendor and invoice.vendor.casefold() != po.vendor.casefold():
        discrepancies.append(Discrepancy(
            reason="VENDOR_MISMATCH", expected=po.vendor, invoiced=invoice.vendor,
            variance="N/A", message="Invoice vendor does not match the purchase order vendor.",
        ))

    po_items, receipt_items, invoice_items = map(_totals, (po.items, receipt.items, invoice.items))
    all_skus = sorted(set(po_items) | set(receipt_items) | set(invoice_items))
    for sku in all_skus:
        ordered = po_items.get(sku)
        received = receipt_items.get(sku)
        billed = invoice_items.get(sku)
        if ordered is None:
            if billed is not None:
                discrepancies.append(Discrepancy(
                    reason="SKU_NOT_ON_PO", sku=sku, expected=Decimal(0),
                    invoiced=billed["qty"], variance=billed["qty"],
                    message=f"SKU {sku} appears on the invoice but not on the purchase order.",
                ))
            continue
        if received is None:
            if billed is not None:
                discrepancies.append(Discrepancy(
                    reason="RECEIPT_ITEM_MISSING", sku=sku, expected=Decimal(0),
                    invoiced=billed["qty"], variance=billed["qty"],
                    message=f"No goods receipt quantity was found for invoiced SKU {sku}.",
                ))
            continue
        if billed is None:
            continue

        if received["qty"] > ordered["qty"] and _outside_tolerance(ordered["qty"], received["qty"], rules.quantity_variance):
            delta = received["qty"] - ordered["qty"]
            discrepancies.append(Discrepancy(
                reason="RECEIPT_EXCEEDS_PO", sku=sku, expected=ordered["qty"],
                invoiced=received["qty"], variance=delta,
                message=f"Received quantity for {sku} exceeds the ordered quantity beyond tolerance.",
            ))
        elif billed["qty"] > received["qty"] and _outside_tolerance(received["qty"], billed["qty"], rules.quantity_variance):
            delta = billed["qty"] - received["qty"]
            discrepancies.append(Discrepancy(
                reason="RECEIPT_QUANTITY_MISMATCH", sku=sku, expected=received["qty"],
                invoiced=billed["qty"], variance=delta,
                message=f"Invoiced quantity for {sku} differs from the received quantity beyond tolerance.",
            ))
        elif billed["qty"] > ordered["qty"] and _outside_tolerance(ordered["qty"], billed["qty"], rules.quantity_variance):
            delta = billed["qty"] - ordered["qty"]
            discrepancies.append(Discrepancy(
                reason="PO_INVOICE_QUANTITY_MISMATCH", sku=sku, expected=ordered["qty"],
                invoiced=billed["qty"], variance=delta,
                message=f"Invoiced quantity for {sku} differs from the ordered quantity beyond tolerance.",
            ))

        po_price, invoice_price = ordered["price"], billed["price"]
        if po_price is not None and invoice_price is not None and po_rate is not None and invoice_rate is not None:
            expected_price, billed_price = po_price * po_rate, invoice_price * invoice_rate
            if _outside_tolerance(expected_price, billed_price, rules.price_variance):
                discrepancies.append(Discrepancy(
                    reason="PRICE_MISMATCH", sku=sku, expected=expected_price,
                    invoiced=billed_price, variance=abs(billed_price - expected_price),
                    message=f"Unit price for {sku} differs from the purchase order beyond tolerance, in {rules.base_currency}.",
                ))
        elif invoice_price is None:
            discrepancies.append(Discrepancy(
                reason="INVOICE_PRICE_MISSING", sku=sku, expected=po_price,
                invoiced="MISSING", variance="N/A", message=f"Invoice unit price is missing for SKU {sku}.",
            ))

    if po.tax_rate is not None and invoice.tax_amount is not None and po_rate is not None and invoice_rate is not None:
        invoice_subtotal = sum(
            (entry["qty"] * (entry["price"] or Decimal(0)) for entry in invoice_items.values()), Decimal(0)
        )
        expected_tax = invoice_subtotal * po.tax_rate * invoice_rate
        actual_tax = _money(invoice.tax_amount, invoice_currency, rules)
        if actual_tax is not None and _outside_tolerance(expected_tax, actual_tax, rules.tax_variance):
            discrepancies.append(Discrepancy(
                reason="TAX_MISMATCH", expected=expected_tax, invoiced=actual_tax,
                variance=abs(actual_tax - expected_tax),
                message=f"Invoice tax differs from the PO tax rate beyond tolerance, in {rules.base_currency}.",
            ))

    status = MatchStatus.EXCEPTION if discrepancies else MatchStatus.MATCHED
    primary = discrepancies[0] if discrepancies else None
    return MatchResult(
        status=status,
        reason=primary.reason if primary else None,
        expected=primary.expected if primary else None,
        invoiced=primary.invoiced if primary else None,
        variance=primary.variance if primary else None,
        po_number=po.number,
        invoice_number=invoice.number,
        vendor=po.vendor,
        base_currency=rules.base_currency,
        discrepancies=discrepancies,
        approval_status=(
            "PENDING" if status == MatchStatus.EXCEPTION
            else "APPROVED" if rules.auto_approve_matches
            else "NOT_REQUIRED"
        ),
    )
