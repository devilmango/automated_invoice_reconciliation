from __future__ import annotations

import os

from .filesystem import FilesystemAPAdapter
from .http import GenericHTTPAPAdapter
from .quickbooks import QuickBooksOnlineAdapter
from .xero import XeroAdapter


def create_adapter(
    name: str,
    *,
    directory: str | None = None,
    endpoint: str | None = None,
    token: str | None = None,
    company_id: str = "default",
):
    if name == "filesystem":
        return FilesystemAPAdapter(directory or os.getenv("AP_ADAPTER_DIRECTORY", "./ap-adapter-data"))
    if name == "http":
        selected_endpoint = endpoint or os.getenv("AP_ADAPTER_URL")
        selected_token = token or os.getenv("AP_ADAPTER_TOKEN")
        if not selected_endpoint or not selected_token:
            raise ValueError("HTTP adapter requires --endpoint/--token or AP_ADAPTER_URL/AP_ADAPTER_TOKEN")
        return GenericHTTPAPAdapter(selected_endpoint, selected_token)
    if name in {"quickbooks", "xero"}:
        config, configured = _company_configuration(name, company_id)
        from ..database import SessionLocal
        session_factory = _company_session_factory(SessionLocal, company_id)
        if configured:
            if name == "xero":
                return XeroAdapter.from_configuration(config, session_factory=session_factory)
            return QuickBooksOnlineAdapter.from_configuration(config, session_factory=session_factory)
        if company_id != "default":
            raise ValueError(f"No enabled {name} connection is configured for company {company_id!r}")
        if name == "xero":
            return XeroAdapter.from_environment(session_factory=session_factory)
        return QuickBooksOnlineAdapter.from_environment(session_factory=session_factory)
    raise ValueError("Adapter must be one of: filesystem, http, quickbooks, xero")


def create_adapter_for_company(company_id: str = "default"):
    from sqlalchemy import select
    from ..database import AccountingConnectionRecord, SessionLocal

    with _company_session_factory(SessionLocal, company_id)() as session:
        connection = session.scalar(select(AccountingConnectionRecord).where(
            AccountingConnectionRecord.enabled.is_(True)
        ).order_by(AccountingConnectionRecord.updated_at.desc()).limit(1))
        if connection is not None:
            return create_adapter(connection.provider, company_id=company_id)
    return create_adapter(os.getenv("AP_DELIVERY_ADAPTER", "filesystem"), company_id=company_id)


def _company_configuration(provider: str, company_id: str) -> tuple[dict | None, bool]:
    from sqlalchemy import select
    from ..accounting_connections import decrypt_configuration
    from ..database import AccountingConnectionRecord, SessionLocal

    with _company_session_factory(SessionLocal, company_id)() as session:
        record = session.scalar(select(AccountingConnectionRecord).where(
            AccountingConnectionRecord.provider == provider
        ))
        if record is None:
            return None, False
        if not record.enabled:
            raise ValueError(f"The {provider} accounting connection is disabled for company {company_id!r}")
        return decrypt_configuration(record.encrypted_config), True


def _company_session_factory(session_factory, company_id: str):
    def create_session():
        session = session_factory()
        session.info["company_id"] = company_id
        return session
    return create_session
