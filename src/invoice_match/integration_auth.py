from __future__ import annotations

import json
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def require_integration(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    raw = os.getenv("INTEGRATION_TOKENS", "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration authentication is not configured",
        )
    try:
        integrations = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration authentication configuration is invalid",
        ) from exc
    if (
        not isinstance(integrations, dict)
        or not integrations
        or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(_token_value(token), str)
            or len(_token_value(token)) < 32
            or not isinstance(_company_value(token), str)
            or not _company_value(token).strip()
            for name, token in integrations.items()
        )
        or len({_token_value(value) for value in integrations.values()}) != len(integrations)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration authentication configuration is invalid",
        )
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An integration bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = credentials.credentials.encode("utf-8")
    matched = [
        name
        for name, value in integrations.items()
        if secrets.compare_digest(presented, _token_value(value).encode("utf-8"))
    ]
    if len(matched) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid integration token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched[0]


def _token_value(value) -> str:
    return value.get("token", "") if isinstance(value, dict) else value


def _company_value(value) -> str:
    if not isinstance(value, dict):
        return "default"
    return value.get("company_id", value.get("company", "default"))


def company_for_token(token: str | None) -> str:
    """Resolve a configured bearer token to its company before the endpoint runs."""
    if not token:
        return "default"
    try:
        integrations = json.loads(os.getenv("INTEGRATION_TOKENS", "{}"))
        reviewers = json.loads(os.getenv("REVIEWER_TOKENS", "{}"))
    except json.JSONDecodeError:
        return "default"
    presented = token.encode("utf-8")
    for name, value in integrations.items() if isinstance(integrations, dict) else ():
        if isinstance(value, (str, dict)) and isinstance(_token_value(value), str) and secrets.compare_digest(
            presented, _token_value(value).encode("utf-8")
        ):
            return _company_value(value)
    for value in reviewers.values() if isinstance(reviewers, dict) else ():
        if isinstance(value, dict) and isinstance(value.get("token"), str) and secrets.compare_digest(
            presented, value["token"].encode("utf-8")
        ):
            return value.get("company_id", value.get("company", "default"))
    return "default"


def configured_company_ids() -> list[str]:
    """Return all companies configured for reviewers or integrations for worker polling."""
    result = {"default"}
    for key in ("INTEGRATION_TOKENS", "REVIEWER_TOKENS"):
        try:
            values = json.loads(os.getenv(key, "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(values, dict):
            continue
        for value in values.values():
            company = _company_value(value) if key == "INTEGRATION_TOKENS" else (
                value.get("company_id", value.get("company", "default")) if isinstance(value, dict) else "default"
            )
            if isinstance(company, str) and company.strip():
                result.add(company.strip())
    return sorted(result)
