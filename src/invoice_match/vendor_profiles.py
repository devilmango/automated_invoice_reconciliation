from __future__ import annotations

import os
import re
from pathlib import Path

import yaml


def normalize_name(value: str | None) -> str:
    return "".join(character for character in (value or "").casefold() if character.isalnum())


def load_vendor_profiles() -> list[dict]:
    configured = os.getenv("INVOICE_MATCH_VENDOR_PROFILES")
    project_default = Path(__file__).resolve().parents[2] / "config" / "vendor_profiles.yaml"
    path = Path(configured) if configured else project_default
    if not path.exists():
        return []
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not load vendor profiles: {exc}") from exc
    vendors = payload.get("vendors", [])
    if not isinstance(vendors, list):
        raise ValueError("Vendor profile file must contain a 'vendors' list")
    seen: dict[str, str] = {}
    valid_label_fields = {"number", "vendor", "invoice_date", "currency", "tax_amount", "total_amount"}
    profiles = []
    for profile in vendors:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str) or not profile["name"].strip():
            raise ValueError("Each vendor profile requires a non-empty name")
        aliases = profile.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise ValueError(f"Vendor profile {profile['name']!r} aliases must be a list of strings")
        labels = profile.get("labels", {})
        if not isinstance(labels, dict) or set(labels) - valid_label_fields:
            raise ValueError(f"Vendor profile {profile['name']!r} contains unsupported label fields")
        if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values) for values in labels.values()):
            raise ValueError(f"Vendor profile {profile['name']!r} labels must be lists of strings")
        sku_aliases = profile.get("sku_aliases", {})
        if not isinstance(sku_aliases, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in sku_aliases.items()
        ):
            raise ValueError(f"Vendor profile {profile['name']!r} sku_aliases must map strings to strings")
        currency = profile.get("default_currency")
        if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency)):
            raise ValueError(f"Vendor profile {profile['name']!r} default_currency must be a 3-letter code")
        date_format = profile.get("date_format")
        if date_format is not None and not isinstance(date_format, str):
            raise ValueError(f"Vendor profile {profile['name']!r} date_format must be a string")
        normalized = dict(profile)
        normalized["aliases"] = aliases
        normalized["labels"] = labels
        normalized["sku_aliases"] = {key.casefold(): value for key, value in sku_aliases.items()}
        normalized["default_currency"] = currency.upper() if currency else None
        for name in [profile["name"], *aliases]:
            key = normalize_name(name)
            if not key:
                raise ValueError(f"Vendor profile {profile['name']!r} contains an empty alias")
            other = seen.get(key)
            if other and other != profile["name"]:
                raise ValueError(f"Vendor alias {name!r} is shared by {other!r} and {profile['name']!r}")
            seen[key] = profile["name"]
        profiles.append(normalized)
    return profiles


def profile_for_vendor(vendor: str | None, profiles: list[dict] | None = None) -> dict | None:
    key = normalize_name(vendor)
    if not key:
        return None
    for profile in profiles if profiles is not None else load_vendor_profiles():
        if key in {normalize_name(profile["name"]), *(normalize_name(alias) for alias in profile["aliases"])}:
            return profile
    return None


def canonical_vendor(vendor: str | None, profiles: list[dict] | None = None) -> str | None:
    profile = profile_for_vendor(vendor, profiles)
    return profile["name"] if profile else vendor


def vendor_aliases(vendor: str | None, profiles: list[dict] | None = None) -> list[str]:
    profile = profile_for_vendor(vendor, profiles)
    return [profile["name"], *profile["aliases"]] if profile else ([vendor] if vendor else [])


def vendors_equivalent(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    profiles = load_vendor_profiles()
    return normalize_name(canonical_vendor(left, profiles)) == normalize_name(canonical_vendor(right, profiles))
