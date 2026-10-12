from __future__ import annotations

import json
import os
from cryptography.fernet import Fernet, InvalidToken

_PROVIDER_FIELDS = {
    "quickbooks": {"client_id", "client_secret", "realm_id", "refresh_token",
                   "token_encryption_key", "environment", "vendor_map", "item_map",
                   "tax_code_map", "ap_account_id", "freight_account_id", "discount_account_id",
                   "timeout", "minor_version"},
    "xero": {"client_id", "client_secret", "tenant_id", "refresh_token",
             "token_encryption_key", "contact_map", "item_map", "tax_type_map",
             "expense_account_code", "timeout"},
}


def validate_configuration(provider: str, config: dict) -> dict:
    if provider not in _PROVIDER_FIELDS or not isinstance(config, dict):
        raise ValueError("Provider must be quickbooks or xero and configuration must be an object")
    unknown = set(config) - _PROVIDER_FIELDS[provider]
    if unknown:
        raise ValueError(f"Unsupported {provider} configuration field(s): {', '.join(sorted(unknown))}")
    required = {"client_id", "client_secret", "refresh_token", "token_encryption_key"}
    required.add("realm_id" if provider == "quickbooks" else "tenant_id")
    missing = [key for key in required if not isinstance(config.get(key), str) or not config[key].strip()]
    if missing:
        raise ValueError(f"Missing required {provider} configuration field(s): {', '.join(sorted(missing))}")
    if provider == "quickbooks":
        if not config["realm_id"].isdigit():
            raise ValueError("QuickBooks realm_id must be numeric")
        if config.get("environment", "sandbox") not in {"sandbox", "production"}:
            raise ValueError("QuickBooks environment must be sandbox or production")
        maps = ("vendor_map", "item_map", "tax_code_map")
    else:
        maps = ("contact_map", "item_map", "tax_type_map")
    for name in maps:
        value = config.get(name, {})
        if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in value.items()):
            raise ValueError(f"{name} must map names to string identifiers")
    if "timeout" in config and (not isinstance(config["timeout"], (int, float)) or config["timeout"] <= 0):
        raise ValueError("timeout must be a positive number")
    return config


def _fernet() -> Fernet:
    key = os.getenv("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY", "")
    if not key:
        raise ValueError("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY must be configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("ACCOUNTING_CONNECTIONS_ENCRYPTION_KEY must be a valid Fernet key") from exc


def encrypt_configuration(config: dict) -> str:
    return _fernet().encrypt(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).decode()


def decrypt_configuration(value: str) -> dict:
    try:
        raw = _fernet().decrypt(value.encode()).decode()
        config = json.loads(raw)
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not decrypt accounting connection; verify its encryption key") from exc
    if not isinstance(config, dict):
        raise ValueError("Stored accounting connection configuration is invalid")
    return config


def public_connection(record) -> dict:
    config = decrypt_configuration(record.encrypted_config)
    account_id = config.get("realm_id") or config.get("tenant_id")
    return {
        "provider": record.provider,
        "enabled": record.enabled,
        "account_id": account_id,
        "configured_fields": sorted(config.keys()),
        "updated_by": record.updated_by,
        "updated_at": record.updated_at.isoformat(),
    }
