from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ToleranceRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity_variance: Decimal = Decimal("0.02")
    price_variance: Decimal = Decimal("0.01")
    tax_variance: Decimal = Decimal("0.005")
    base_currency: str = "USD"
    rates_to_base: dict[str, Decimal] = Field(default_factory=lambda: {"USD": Decimal(1)})
    auto_approve_matches: bool = False


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
    return ToleranceRules(
        quantity_variance=_ratio(rules.get("quantity_variance", {}).get("tolerance", "2%"), "quantity_variance"),
        price_variance=_ratio(rules.get("price_variance", {}).get("tolerance", "1%"), "price_variance"),
        tax_variance=_ratio(rules.get("tax_variance", {}).get("tolerance", "0.5%"), "tax_variance"),
        base_currency=base,
        rates_to_base=parsed_rates,
        auto_approve_matches=bool(approval.get("auto_approve_matches", False)),
    )
