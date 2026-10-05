from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)
_REVIEWER_ROLES = {"reviewer", "approver", "admin"}


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str]


def _reviewers() -> dict[str, dict]:
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
    if not isinstance(reviewers, dict) or not reviewers:
        _invalid_configuration()
    for name, entry in reviewers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            _invalid_configuration()
        token, roles = entry.get("token"), entry.get("roles")
        if (
            not isinstance(token, str)
            or len(token) < 32
            or not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or role not in _REVIEWER_ROLES for role in roles)
            or len(set(roles)) != len(roles)
        ):
            _invalid_configuration()
    tokens = [entry["token"] for entry in reviewers.values()]
    if len(set(tokens)) != len(tokens):
        _invalid_configuration()
    return reviewers


def _invalid_configuration() -> None:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Reviewer authentication configuration is invalid",
    )


def authenticate_reviewer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    reviewers = _reviewers()
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A reviewer bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = credentials.credentials.encode("utf-8")
    matched = [
        (name, entry)
        for name, entry in reviewers.items()
        if secrets.compare_digest(presented, entry["token"].encode("utf-8"))
    ]
    if len(matched) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    name, entry = matched[0]
    return Principal(name=name, roles=frozenset(entry["roles"]))


def require_reviewer(principal: Principal = Depends(authenticate_reviewer)) -> Principal:
    return principal


def require_approver(principal: Principal = Depends(authenticate_reviewer)) -> Principal:
    if not principal.roles.intersection({"approver", "admin"}):
        raise HTTPException(status_code=403, detail="An approver role is required")
    return principal


def require_admin(principal: Principal = Depends(authenticate_reviewer)) -> Principal:
    if "admin" not in principal.roles:
        raise HTTPException(status_code=403, detail="An admin role is required")
    return principal
