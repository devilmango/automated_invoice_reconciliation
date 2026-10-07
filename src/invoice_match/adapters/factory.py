from __future__ import annotations

import os

from .filesystem import FilesystemAPAdapter
from .http import GenericHTTPAPAdapter
from .quickbooks import QuickBooksOnlineAdapter


def create_adapter(
    name: str,
    *,
    directory: str | None = None,
    endpoint: str | None = None,
    token: str | None = None,
):
    if name == "filesystem":
        return FilesystemAPAdapter(directory or os.getenv("AP_ADAPTER_DIRECTORY", "./ap-adapter-data"))
    if name == "http":
        selected_endpoint = endpoint or os.getenv("AP_ADAPTER_URL")
        selected_token = token or os.getenv("AP_ADAPTER_TOKEN")
        if not selected_endpoint or not selected_token:
            raise ValueError("HTTP adapter requires --endpoint/--token or AP_ADAPTER_URL/AP_ADAPTER_TOKEN")
        return GenericHTTPAPAdapter(selected_endpoint, selected_token)
    if name == "quickbooks":
        from ..database import SessionLocal

        return QuickBooksOnlineAdapter.from_environment(session_factory=SessionLocal)
    raise ValueError("Adapter must be one of: filesystem, http, quickbooks")
