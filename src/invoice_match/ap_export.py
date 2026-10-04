from __future__ import annotations

from decimal import Decimal

from .financials import invoice_summary, line_net_amount, round_currency
from .rules import ToleranceRules
from .schemas import APExportLine, APExportPayload, MatchRequest


def build_ap_payload(match_id: str, request: MatchRequest, rules: ToleranceRules) -> dict:
    invoice = request.invoice
    summary = invoice_summary(request, rules)
    invoice_lines = []
    for line in invoice.items:
        po_lines = [item for item in request.po.items if item.sku == line.sku]
        po_rates = {
            item.tax_rate if item.tax_rate is not None else request.po.tax_rate
            for item in po_lines
        }
        rate = next(iter(po_rates)) if len(po_rates) == 1 else line.tax_rate
        if rate is None:
            rate = line.tax_rate
        line_tax = line.tax_amount
        if line_tax is None:
            line_tax = line_net_amount(line) * (rate or Decimal(0))
        invoice_lines.append(APExportLine(
            sku=line.sku,
            description=line.description,
            quantity=line.qty,
            unit_price=line.price or Decimal(0),
            discount_rate=line.discount_rate,
            tax_code=line.tax_code,
            tax_rate=rate,
            line_net=round_currency(line_net_amount(line), invoice.currency, rules),
            line_tax=round_currency(line_tax, invoice.currency, rules),
        ))
    return APExportPayload(
        match_id=match_id,
        purchase_order_number=request.po.number,
        supplier_invoice_number=invoice.number or "",
        supplier=invoice.vendor or request.po.vendor,
        currency=invoice.currency.upper(),
        cost_center=request.po.cost_center,
        subtotal=summary["subtotal"],
        freight_amount=summary["freight"],
        discount_amount=summary["discount"],
        tax_amount=summary["tax"],
        total_amount=summary["total"],
        lines=invoice_lines,
        approval_status="APPROVED",
    ).model_dump(mode="json")
