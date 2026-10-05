from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    min_invoice_total: Decimal | None = Field(default=None, ge=0)
    max_invoice_total: Decimal | None = Field(default=None, ge=0)
    min_variance_amount: Decimal | None = Field(default=None, ge=0)
    max_variance_amount: Decimal | None = Field(default=None, ge=0)
    vendor: str | None = None
    cost_center: str | None = None
    required_role: Literal["approver", "admin"] = "approver"
    approvals_required: int = Field(default=1, ge=1, le=10)
    assigned_to: str | None = None
    sla_hours: int = Field(default=72, ge=1, le=8760)

    def matches(
        self,
        *,
        vendor: str,
        cost_center: str | None,
        invoice_total: Decimal,
        variance_amount: Decimal,
    ) -> bool:
        def normalized(value: str | None) -> str | None:
            return " ".join(value.casefold().split()) if value else None

        return all((
            self.min_invoice_total is None or invoice_total >= self.min_invoice_total,
            self.max_invoice_total is None or invoice_total <= self.max_invoice_total,
            self.min_variance_amount is None or variance_amount >= self.min_variance_amount,
            self.max_variance_amount is None or variance_amount <= self.max_variance_amount,
            self.vendor is None or normalized(vendor) == normalized(self.vendor),
            self.cost_center is None or normalized(cost_center) == normalized(self.cost_center),
        ))


class QuantityUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str
    to_base: Decimal = Field(gt=0)


class ToleranceRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity_variance: Decimal = Decimal("0.02")
    price_variance: Decimal = Decimal("0.01")
    tax_variance: Decimal = Decimal("0.005")
    amount_variance: Decimal = Decimal("0.005")
    base_currency: str = "USD"
    rates_to_base: dict[str, Decimal] = Field(default_factory=lambda: {"USD": Decimal(1)})
    minor_units: dict[str, int] = Field(default_factory=lambda: {"USD": 2})
    quantity_units: dict[str, QuantityUnit] = Field(default_factory=lambda: {
        "EA": QuantityUnit(family="count", to_base=Decimal(1)),
    })
    auto_approve_matches: bool = False
    approval_policies: list[ApprovalPolicy] = Field(default_factory=list)


def select_approval_policy(
    rules: ToleranceRules,
    *,
    vendor: str,
    cost_center: str | None,
    invoice_total: Decimal,
    variance_amount: Decimal,
) -> ApprovalPolicy:
    for policy in rules.approval_policies:
        if policy.matches(
            vendor=vendor,
            cost_center=cost_center,
            invoice_total=invoice_total,
            variance_amount=variance_amount,
        ):
            return policy
    return ApprovalPolicy(name="default")


def _ratio(value: object, key: str) -> Decimal:
    """Parse rule values like 2%, .02, or 2 (meaning 2 percent)."""
    raw = str(value).strip()
    try:
        result = Decimal(raw[:-1]) / 100 if raw.endswith("%") else Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"rules.{key}.tolerance must be a percentage") from exc
    if not raw.endswith("%") and result > 1:
        result /= 100
    if result < 0 or result > 1:
        raise ValueError(f"rules.{key}.tolerance must be between 0% and 100%")
    return result


def load_rules(path: str | Path | None = None) -> ToleranceRules:
    selected = Path(path or os.getenv("INVOICE_MATCH_RULES", "config/rules.yaml"))
    if not selected.exists():
        return ToleranceRules()
    data = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    rules = data.get("rules", {})
    currencies = rules.get("currency", {})
    approval = rules.get("approval", {})
    rates = currencies.get("rates_to_base", {"USD": 1})
    try:
        parsed_rates = {str(code).upper(): Decimal(str(rate)) for code, rate in rates.items()}
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("rules.currency.rates_to_base must map currency codes to numbers") from exc
    base = str(currencies.get("base_currency", "USD")).upper()
    if base not in parsed_rates or parsed_rates.get(base) != Decimal(1) or any(rate <= 0 for rate in parsed_rates.values()):
        raise ValueError("currency rates must be positive and set the base currency rate to 1")
    minor_units = {str(code).upper(): int(value) for code, value in currencies.get("minor_units", {}).items()}
    if any(places < 0 or places > 6 for places in minor_units.values()):
        raise ValueError("currency minor-unit precision must be between 0 and 6")
    minor_units.setdefault(base, 2)

    quantity_units = {
        str(code).upper(): QuantityUnit.model_validate(definition)
        for code, definition in rules.get("quantity_units", {}).items()
    }
    quantity_units.setdefault("EA", QuantityUnit(family="count", to_base=Decimal(1)))

    policies = [ApprovalPolicy.model_validate(policy) for policy in approval.get("policies", [])]
    return ToleranceRules(
        quantity_variance=_ratio(rules.get("quantity_variance", {}).get("tolerance", "2%"), "quantity_variance"),
        price_variance=_ratio(rules.get("price_variance", {}).get("tolerance", "1%"), "price_variance"),
        tax_variance=_ratio(rules.get("tax_variance", {}).get("tolerance", "0.5%"), "tax_variance"),
        amount_variance=_ratio(rules.get("amount_variance", {}).get("tolerance", "0.5%"), "amount_variance"),
        base_currency=base,
        rates_to_base=parsed_rates,
        minor_units=minor_units,
        quantity_units=quantity_units,
        auto_approve_matches=bool(approval.get("auto_approve_matches", False)),
        approval_policies=policies,
    )
