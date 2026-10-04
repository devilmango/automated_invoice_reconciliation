from __future__ import annotations

import json
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def _reviewers() -> dict[str, str]:
    raw = os.getenv("REVIEWER_TOKENS", "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication is not configured",
        )
    try:
        reviewers = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication configuration is invalid",
        ) from exc
    if (
        not isinstance(reviewers, dict)
        or not reviewers
        or any(not isinstance(name, str) or not name.strip() for name in reviewers)
        or any(not isinstance(token, str) or len(token) < 32 for token in reviewers.values())
        or len(set(reviewers.values())) != len(reviewers)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication configuration is invalid",
        )
    return reviewers


def require_reviewer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    reviewers = _reviewers()
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A reviewer bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = credentials.credentials.encode("utf-8")
    matched = [
        name
        for name, token in reviewers.items()
        if secrets.compare_digest(presented, token.encode("utf-8"))
    ]
    if len(matched) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched[0]
