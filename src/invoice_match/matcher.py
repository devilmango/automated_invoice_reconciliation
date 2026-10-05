from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .financials import invoice_summary, item_tax_rate, round_currency, to_base_currency
from .rules import ToleranceRules, select_approval_policy
from .schemas import Discrepancy, MatchRequest, MatchResult, MatchStatus, SupplierInvoice


def _unit(item, rules: ToleranceRules) -> tuple[str, Decimal]:
    configured = rules.quantity_units.get(item.unit.strip().upper())
    if configured is None:
        return item.unit.strip().casefold(), Decimal(1)
    return configured.family.casefold(), configured.to_base


def _totals(items: list, rules: ToleranceRules, default_tax_rate: Decimal | None = None) -> dict[str, dict]:
    grouped: dict[str, dict] = defaultdict(lambda: {
        "qty": Decimal(0),
        "priced_qty": Decimal(0),
        "net_amount": Decimal(0),
        "tax_amount_basis": Decimal(0),
        "tax_amount_weight": Decimal(0),
        "tax_codes": set(),
        "unit_families": set(),
    })
    for item in items:
        values = grouped[item.sku]
        family, factor = _unit(item, rules)
        values["qty"] += item.qty * factor
        values["unit_families"].add(family)
        if item.tax_code:
            values["tax_codes"].add(item.tax_code.strip().casefold())
        if item.price is not None:
            net_unit_price = item.price * (Decimal(1) - item.discount_rate)
            net_amount = item.qty * net_unit_price
            values["priced_qty"] += item.qty * factor
            values["net_amount"] += net_amount
            rate = item.tax_rate if item.tax_rate is not None else default_tax_rate
            if rate is not None:
                values["tax_amount_basis"] += net_amount * rate
                values["tax_amount_weight"] += net_amount
    return {
        sku: {
            "qty": values["qty"],
            "net_price": values["net_amount"] / values["priced_qty"] if values["priced_qty"] else None,
            "tax_rate": (
                values["tax_amount_basis"] / values["tax_amount_weight"]
                if values["tax_amount_weight"] else None
            ),
            "tax_codes": values["tax_codes"],
            "unit_families": values["unit_families"],
        }
        for sku, values in grouped.items()
    }


def _outside_tolerance(expected: Decimal, actual: Decimal, tolerance: Decimal) -> bool:
    return abs(actual - expected) > abs(expected) * tolerance


def _discrepancy(
    reason: str,
    message: str,
    expected: Decimal | str,
    invoiced: Decimal | str,
    *,
    sku: str | None = None,
) -> Discrepancy:
    variance = abs(invoiced - expected) if isinstance(expected, Decimal) and isinstance(invoiced, Decimal) else "N/A"
    return Discrepancy(
        reason=reason,
        sku=sku,
        expected=expected,
        invoiced=invoiced,
        variance=variance,
        message=message,
    )


def match_documents(
    request: MatchRequest,
    rules: ToleranceRules,
    *,
    previously_invoiced: list[SupplierInvoice] | None = None,
) -> MatchResult:
    previously_invoiced = previously_invoiced or []
    po, receipt, invoice = request.po, request.receipt, request.invoice
    discrepancies: list[Discrepancy] = []
    po_currency, invoice_currency = po.currency.upper(), invoice.currency.upper()
    po_rate = rules.rates_to_base.get(po_currency)
    invoice_rate = rules.rates_to_base.get(invoice_currency)

    if po_rate is None or invoice_rate is None:
        discrepancies.append(_discrepancy(
            "CURRENCY_MISMATCH",
            "A currency is missing from rules.currency.rates_to_base.",
            po_currency,
            invoice_currency,
        ))
    if invoice.vendor and invoice.vendor.casefold() != po.vendor.casefold():
        discrepancies.append(_discrepancy(
            "VENDOR_MISMATCH",
            "Invoice vendor does not match the purchase order vendor.",
            po.vendor,
            invoice.vendor,
        ))

    po_items = _totals(po.items, rules, po.tax_rate)
    receipt_items = _totals(receipt.all_items(), rules)
    invoice_items = _totals(invoice.items, rules)
    prior_invoiced: dict[str, Decimal] = defaultdict(Decimal)
    prior_unit_families: dict[str, set[str]] = defaultdict(set)
    creditable_by_invoice: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    prior_invoice_numbers: set[str] = set()
    for prior in previously_invoiced:
        prior_totals = _totals(prior.items, rules)
        direction = Decimal(-1) if prior.document_type == "CREDIT_NOTE" else Decimal(1)
        for sku, values in prior_totals.items():
            prior_invoiced[sku] += values["qty"] * direction
            prior_unit_families[sku].update(values["unit_families"])
            if prior.document_type == "INVOICE" and prior.number:
                prior_invoice_numbers.add(prior.number)
                creditable_by_invoice[prior.number][sku] += values["qty"]
            elif prior.document_type == "CREDIT_NOTE" and prior.credit_note_for:
                creditable_by_invoice[prior.credit_note_for][sku] -= values["qty"]
    if invoice.document_type == "CREDIT_NOTE" and invoice.credit_note_for not in prior_invoice_numbers:
        discrepancies.append(_discrepancy(
            "CREDIT_NOTE_REFERENCE_NOT_FOUND",
            "Credit note must reference a previously submitted invoice that is not rejected.",
            invoice.credit_note_for or "MISSING",
            invoice.number or "UNKNOWN",
        ))
    for sku in sorted(set(po_items) | set(receipt_items) | set(invoice_items)):
        ordered, received, billed = po_items.get(sku), receipt_items.get(sku), invoice_items.get(sku)
        if ordered is None:
            if billed is not None:
                discrepancies.append(_discrepancy(
                    "SKU_NOT_ON_PO", f"SKU {sku} appears on the invoice but not on the purchase order.",
                    Decimal(0), billed["qty"], sku=sku,
                ))
            continue
        if received is None:
            if billed is not None:
                discrepancies.append(_discrepancy(
                    "RECEIPT_ITEM_MISSING", f"No goods receipt quantity was found for invoiced SKU {sku}.",
                    Decimal(0), billed["qty"], sku=sku,
                ))
            continue
        if billed is None:
            continue

        all_families = (
            ordered["unit_families"]
            | received["unit_families"]
            | billed["unit_families"]
            | prior_unit_families[sku]
        )
        if len(all_families) > 1:
            discrepancies.append(_discrepancy(
                "UOM_MISMATCH",
                f"Units of measure for {sku} do not share a configured quantity unit family.",
                ",".join(sorted(ordered["unit_families"] | received["unit_families"] | prior_unit_families[sku])),
                ",".join(sorted(billed["unit_families"])),
                sku=sku,
            ))
            continue

        prior_qty = prior_invoiced[sku]
        if invoice.document_type == "CREDIT_NOTE":
            creditable_qty = creditable_by_invoice[invoice.credit_note_for or ""][sku]
            if creditable_qty < billed["qty"]:
                discrepancies.append(_discrepancy(
                    "CREDIT_NOTE_EXCEEDS_INVOICED",
                    f"Credit note quantity for {sku} exceeds the referenced invoice's remaining quantity.",
                    creditable_qty,
                    billed["qty"],
                    sku=sku,
                ))
            creditable_by_invoice[invoice.credit_note_for or ""][sku] -= billed["qty"]
            cumulative_billed = prior_qty - billed["qty"]
        else:
            cumulative_billed = prior_qty + billed["qty"]

        if received["qty"] > ordered["qty"] and _outside_tolerance(
            ordered["qty"], received["qty"], rules.quantity_variance
        ):
            discrepancies.append(_discrepancy(
                "RECEIPT_EXCEEDS_PO", f"Received quantity for {sku} exceeds the ordered quantity beyond tolerance.",
                ordered["qty"], received["qty"], sku=sku,
            ))
        elif invoice.document_type == "INVOICE" and cumulative_billed > received["qty"] and _outside_tolerance(
            received["qty"], cumulative_billed, rules.quantity_variance
        ):
            discrepancies.append(_discrepancy(
                "RECEIPT_QUANTITY_MISMATCH", f"Cumulative invoiced quantity for {sku} exceeds the received quantity beyond tolerance.",
                received["qty"], cumulative_billed, sku=sku,
            ))
        elif invoice.document_type == "INVOICE" and cumulative_billed > ordered["qty"] and _outside_tolerance(
            ordered["qty"], cumulative_billed, rules.quantity_variance
        ):
            discrepancies.append(_discrepancy(
                "PO_INVOICE_QUANTITY_MISMATCH", f"Cumulative invoiced quantity for {sku} exceeds the ordered quantity beyond tolerance.",
                ordered["qty"], cumulative_billed, sku=sku,
            ))

        po_price, invoice_price = ordered["net_price"], billed["net_price"]
        if po_price is not None and invoice_price is not None and po_rate is not None and invoice_rate is not None:
            expected_price = round_currency(po_price * po_rate, rules.base_currency, rules)
            billed_price = round_currency(invoice_price * invoice_rate, rules.base_currency, rules)
            if _outside_tolerance(expected_price, billed_price, rules.price_variance):
                discrepancies.append(_discrepancy(
                    "PRICE_MISMATCH",
                    f"Net unit price for {sku} differs from the purchase order beyond tolerance, in {rules.base_currency}.",
                    expected_price, billed_price, sku=sku,
                ))
        elif invoice_price is None:
            discrepancies.append(_discrepancy(
                "INVOICE_PRICE_MISSING", f"Invoice unit price is missing for SKU {sku}.",
                ordered["net_price"] or "UNKNOWN", "MISSING", sku=sku,
            ))

        if ordered["tax_codes"] and billed["tax_codes"] and ordered["tax_codes"].isdisjoint(billed["tax_codes"]):
            discrepancies.append(_discrepancy(
                "TAX_CODE_MISMATCH", f"Tax code for {sku} differs from the purchase order.",
                ",".join(sorted(ordered["tax_codes"])), ",".join(sorted(billed["tax_codes"])), sku=sku,
            ))
        if ordered["tax_rate"] is not None and billed["tax_rate"] is not None:
            if _outside_tolerance(ordered["tax_rate"], billed["tax_rate"], rules.tax_variance):
                discrepancies.append(_discrepancy(
                    "TAX_RATE_MISMATCH", f"Tax rate for {sku} differs from the purchase order beyond tolerance.",
                    ordered["tax_rate"], billed["tax_rate"], sku=sku,
                ))

    summary = invoice_summary(request, rules)
    if po_rate is not None and invoice_rate is not None:
        for reason, expected_doc, actual_doc, label in (
            ("FREIGHT_MISMATCH", po.freight_amount, invoice.freight_amount, "Freight"),
            ("DOCUMENT_DISCOUNT_MISMATCH", po.discount_amount, invoice.discount_amount, "Document discount"),
        ):
            expected_amount = to_base_currency(expected_doc, po_currency, rules)
            actual_amount = to_base_currency(actual_doc, invoice_currency, rules)
            if expected_amount is not None and actual_amount is not None and _outside_tolerance(
                expected_amount, actual_amount, rules.price_variance
            ):
                discrepancies.append(_discrepancy(
                    reason, f"{label} differs from the purchase order beyond tolerance, in {rules.base_currency}.",
                    expected_amount, actual_amount,
                ))

        expected_tax_parts: list[Decimal] = []
        tax_rates_known = True
        for item in invoice.items:
            po_tax_rate = item_tax_rate(item, po_items.get(item.sku), po.tax_rate)
            if po_tax_rate is None:
                tax_rates_known = False
                break
            expected_tax_parts.append(
                item.qty * (item.price or Decimal(0)) * (Decimal(1) - item.discount_rate) * po_tax_rate
            )
        invoice_reports_tax = invoice.tax_amount is not None or any(
            item.tax_amount is not None for item in invoice.items
        )
        if tax_rates_known and invoice_reports_tax:
            rounded_expected_tax = round_currency(sum(expected_tax_parts, Decimal(0)), invoice_currency, rules)
            expected_tax = to_base_currency(rounded_expected_tax, invoice_currency, rules)
            actual_tax_doc = invoice.tax_amount
            if actual_tax_doc is None:
                actual_tax_doc = sum(
                    (item.tax_amount or Decimal(0) for item in invoice.items), Decimal(0)
                )
            actual_tax = to_base_currency(actual_tax_doc, invoice_currency, rules)
            if expected_tax is not None and actual_tax is not None and _outside_tolerance(
                expected_tax, actual_tax, rules.tax_variance
            ):
                discrepancies.append(_discrepancy(
                    "TAX_MISMATCH",
                    f"Invoice tax differs from the PO tax rules beyond tolerance, in {rules.base_currency}.",
                    expected_tax, actual_tax,
                ))

        po_tax_is_known = po.tax_rate is not None or all(item.tax_rate is not None for item in po.items)
        if invoice.total_amount is not None and (invoice_reports_tax or po_tax_is_known):
            calculated_total = to_base_currency(summary["calculated_total"], invoice_currency, rules)
            reported_total = to_base_currency(summary["total"], invoice_currency, rules)
            if calculated_total is not None and reported_total is not None and _outside_tolerance(
                calculated_total, reported_total, rules.amount_variance
            ):
                discrepancies.append(_discrepancy(
                    "INVOICE_TOTAL_MISMATCH",
                    f"Invoice total does not equal its rounded line, tax, freight, and discount amounts, in {rules.base_currency}.",
                    calculated_total, reported_total,
                ))

    invoice_total_base = to_base_currency(summary["total"], invoice_currency, rules) or Decimal(0)
    monetary_reasons = {
        "TAX_MISMATCH", "FREIGHT_MISMATCH", "DOCUMENT_DISCOUNT_MISMATCH", "INVOICE_TOTAL_MISMATCH"
    }
    variance_amount = Decimal(0)
    for issue in discrepancies:
        if not isinstance(issue.variance, Decimal):
            continue
        if issue.reason == "PRICE_MISMATCH" and issue.sku in invoice_items:
            variance_amount += issue.variance * invoice_items[issue.sku]["qty"]
        elif issue.reason in {
            "RECEIPT_QUANTITY_MISMATCH", "PO_INVOICE_QUANTITY_MISMATCH", "RECEIPT_EXCEEDS_PO"
        } and issue.sku in invoice_items:
            line_price = invoice_items[issue.sku]["net_price"] or Decimal(0)
            base_line_price = line_price * (invoice_rate or Decimal(0))
            variance_amount += issue.variance * base_line_price
        elif issue.reason in monetary_reasons:
            variance_amount += issue.variance

    policy = select_approval_policy(
        rules,
        vendor=po.vendor,
        cost_center=po.cost_center,
        invoice_total=abs(invoice_total_base),
        variance_amount=variance_amount,
    )
    status = MatchStatus.EXCEPTION if discrepancies else MatchStatus.MATCHED
    primary = discrepancies[0] if discrepancies else None
    due_at = (
        datetime.now(timezone.utc) + timedelta(hours=policy.sla_hours)
        if status == MatchStatus.EXCEPTION
        else None
    )
    return MatchResult(
        status=status,
        reason=primary.reason if primary else None,
        expected=primary.expected if primary else None,
        invoiced=primary.invoiced if primary else None,
        variance=primary.variance if primary else None,
        po_number=po.number,
        po_revision=po.revision,
        invoice_number=invoice.number,
        vendor=po.vendor,
        base_currency=rules.base_currency,
        invoice_total=invoice_total_base,
        discrepancies=discrepancies,
        approval_status=(
            "PENDING" if status == MatchStatus.EXCEPTION
            else "APPROVED" if rules.auto_approve_matches
            else "NOT_REQUIRED"
        ),
        approval_policy=policy.name,
        required_role=policy.required_role,
        approvals_required=policy.approvals_required,
        assigned_to=policy.assigned_to,
        due_at=due_at,
    )
