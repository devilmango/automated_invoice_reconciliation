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
            or not isinstance(token, str)
            or len(token) < 32
            for name, token in integrations.items()
        )
        or len(set(integrations.values())) != len(integrations)
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
        for name, token in integrations.items()
        if secrets.compare_digest(presented, token.encode("utf-8"))
    ]
    if len(matched) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid integration token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched[0]
