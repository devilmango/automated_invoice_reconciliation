from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import typer

from .matcher import match_documents
from .parsers import parse_goods_receipt, parse_purchase_order, parse_supplier_invoice
from .rules import load_rules
from .schemas import GoodsReceipt, MatchRequest

app = typer.Typer(help="Three-way invoice matching engine.")


@app.command()
def match(
    po: Path = typer.Option(..., exists=True, readable=True, help="Purchase order JSON file."),
    receipt: Path = typer.Option(..., exists=True, readable=True, help="Goods receipt JSON file."),
    invoice: Path = typer.Option(..., exists=True, readable=True, help="Supplier invoice JSON file."),
    rules: Path | None = typer.Option(None, exists=True, readable=True, help="YAML rules file."),
):
    """Match one PO, goods receipt, and supplier invoice from JSON files."""
    request = MatchRequest(
        po=parse_purchase_order(po),
        receipt=parse_goods_receipt(receipt),
        invoice=parse_supplier_invoice(invoice),
    )
    result = match_documents(request, load_rules(rules))
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if result.status.value == "EXCEPTION":
        raise typer.Exit(code=2)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, min=1, max=65535, help="Port."),
):
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("invoice_match.api:app", host=host, port=port)


@app.command("match-ap")
def match_from_ap(
    po_number: str = typer.Option(..., help="Purchase order identifier in the AP provider."),
    invoice: Path = typer.Option(..., exists=True, readable=True, help="Supplier invoice JSON file."),
    adapter: str = typer.Option("filesystem", help="Source adapter: filesystem or http (QuickBooks is export-only)."),
    directory: Path | None = typer.Option(None, help="Filesystem adapter fixture/export directory."),
    endpoint: str | None = typer.Option(None, help="Generic HTTP adapter base URL."),
    token: str | None = typer.Option(None, help="Generic HTTP adapter bearer token."),
    rules: Path | None = typer.Option(None, exists=True, readable=True, help="YAML rules file."),
):
    """Fetch a PO and its receipts through an AP adapter, then match an invoice."""
    from .adapters import create_adapter

    try:
        provider = create_adapter(adapter, directory=str(directory) if directory else None,
                                  endpoint=endpoint, token=token)
        if not callable(getattr(provider, "fetch_purchase_order", None)) or not callable(
            getattr(provider, "fetch_receipts", None)
        ):
            raise ValueError(f"The {adapter!r} adapter exports payables but cannot fetch PO/receipt documents")
        po_data = provider.fetch_purchase_order(po_number)
        receipt_data = provider.fetch_receipts(po_number)
        if not receipt_data:
            raise ValueError(f"AP adapter returned no goods receipts for {po_number}")
        po = parse_purchase_order_data(po_data)
        receipt = GoodsReceipt.model_validate({"receipts": receipt_data})
        invoice_data = parse_supplier_invoice(invoice)
        result = match_documents(MatchRequest(po=po, receipt=receipt, invoice=invoice_data), load_rules(rules))
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if result.status.value == "EXCEPTION":
        raise typer.Exit(code=2)


@app.command("procurement-sync")
def procurement_sync(
    company_id: str = typer.Option("default", help="Company scope for this sync run."),
    adapter: str = typer.Option(None, help="Source adapter; defaults to AP_PROCUREMENT_ADAPTER or filesystem."),
    directory: Path | None = typer.Option(None, help="Filesystem adapter fixture directory."),
    endpoint: str | None = typer.Option(None, help="Generic HTTP adapter base URL."),
    token: str | None = typer.Option(None, help="Generic HTTP adapter bearer token."),
):
    """Fetch and persist current POs and receipts; schedule this command for recurring sync."""
    from .adapters import create_adapter
    from .database import SessionLocal
    from .procurement_sync import sync_procurement as run_sync

    adapter_name = adapter or os.getenv("AP_PROCUREMENT_ADAPTER", "filesystem")
    try:
        provider = create_adapter(
            adapter_name,
            directory=str(directory) if directory else None,
            endpoint=endpoint,
            token=token,
            company_id=company_id,
        )
        if not callable(getattr(provider, "list_purchase_orders", None)) or not callable(
            getattr(provider, "fetch_receipts", None)
        ):
            raise ValueError(f"The {adapter_name!r} adapter cannot sync procurement documents")
        with SessionLocal() as session:
            session.info["company_id"] = company_id
            summary = run_sync(session, provider)
    except (ValueError, RuntimeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(summary, indent=2))
    if summary["failed"]:
        raise typer.Exit(code=1)


@app.command("capture-inbox")
def capture_inbox(
    directory: Path | None = typer.Option(None, envvar="INVOICE_MATCH_CAPTURE_INBOX_DIRECTORY",
                                          exists=True, file_okay=False, readable=True,
                                          help="Folder containing PDF, EML, or text invoice files."),
    api_url: str = typer.Option(None, help="Invoice Match API base URL (default: INVOICE_MATCH_API_URL)."),
    token: str = typer.Option(None, envvar="INVOICE_MATCH_INTEGRATION_TOKEN",
                              help="Integration bearer token."),
    limit: int = typer.Option(100, min=1, max=1000, help="Maximum files to process per run."),
):
    """Capture files in an inbox directory and archive accepted or duplicate documents."""
    if directory is None:
        typer.echo("Provide --directory or set INVOICE_MATCH_CAPTURE_INBOX_DIRECTORY", err=True)
        raise typer.Exit(code=1)
    base_url = api_url or os.getenv("INVOICE_MATCH_API_URL", "http://127.0.0.1:8000")
    if not token:
        typer.echo("Provide --token or set INVOICE_MATCH_INTEGRATION_TOKEN", err=True)
        raise typer.Exit(code=1)
    if not base_url.startswith(("http://", "https://")):
        typer.echo("API URL must start with http:// or https://", err=True)
        raise typer.Exit(code=1)
    summary = _capture_inbox(directory, base_url, token, limit)
    typer.echo(json.dumps(summary, indent=2))
    if summary["failed"] or summary["rejected"]:
        raise typer.Exit(code=1)


def _capture_inbox(directory: Path, api_url: str, token: str, limit: int) -> dict:
    supported_types = {
        ".pdf": "application/pdf",
        ".eml": "message/rfc822",
        ".txt": "text/plain",
    }
    summary = {"scanned": 0, "captured": 0, "duplicates": 0, "failed": 0,
               "rejected": 0, "items": []}
    processed_dir = directory / "processed"
    rejected_dir = directory / "rejected"
    processed_dir.mkdir(exist_ok=True)
    rejected_dir.mkdir(exist_ok=True)
    endpoint = urljoin(api_url.rstrip("/") + "/", "documents/extract")
    candidates = [path for path in sorted(directory.iterdir()) if path.is_file() and not path.name.startswith(".")]
    for path in candidates[:limit]:
        summary["scanned"] += 1
        content_type = supported_types.get(path.suffix.casefold())
        if content_type is None:
            summary["rejected"] += 1
            summary["items"].append({"filename": path.name, "state": "REJECTED", "error": "Unsupported file type"})
            _archive_capture(path, rejected_dir)
            continue
        if path.stat().st_size > 10 * 1024 * 1024:
            summary["rejected"] += 1
            summary["items"].append({"filename": path.name, "state": "REJECTED", "error": "Document exceeds 10 MiB"})
            _archive_capture(path, rejected_dir)
            continue
        request = Request(
            endpoint,
            data=path.read_bytes(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
                "X-Filename": path.name,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
            payload = json.loads(body) if body else {}
            summary["captured"] += 1
            summary["items"].append({
                "filename": path.name,
                "state": "CAPTURED",
                "document_id": payload.get("document_id"),
            })
            _archive_capture(path, processed_dir)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code == 409:
                try:
                    document_id = json.loads(detail).get("detail", {}).get("document_id")
                except (json.JSONDecodeError, AttributeError):
                    document_id = None
                summary["duplicates"] += 1
                summary["items"].append({"filename": path.name, "state": "DUPLICATE", "document_id": document_id})
                _archive_capture(path, processed_dir)
            elif 400 <= exc.code < 500 and exc.code != 429:
                summary["rejected"] += 1
                summary["items"].append({"filename": path.name, "state": "REJECTED", "error": detail})
                _archive_capture(path, rejected_dir)
            else:
                summary["failed"] += 1
                summary["items"].append({"filename": path.name, "state": "RETRY", "error": detail})
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            summary["failed"] += 1
            summary["items"].append({"filename": path.name, "state": "RETRY", "error": str(exc)[:500]})
    return summary


def _archive_capture(source: Path, destination: Path) -> None:
    target = destination / source.name
    if target.exists():
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        target = destination / f"{source.stem}-{digest}{source.suffix}"
    source.replace(target)


@app.command()
def deliver(
    company_id: str = typer.Option("default", help="Company scope for this delivery run."),
    adapter: str = typer.Option("filesystem", help="AP adapter: filesystem, http, or quickbooks."),
    directory: Path | None = typer.Option(None, help="Filesystem adapter fixture/export directory."),
    endpoint: str | None = typer.Option(None, help="Generic HTTP adapter base URL."),
    token: str | None = typer.Option(None, help="Generic HTTP adapter bearer token."),
    limit: int = typer.Option(100, min=1, max=1000, help="Maximum outbox entries to deliver."),
):
    """Deliver pending AP outbox entries through an adapter, retaining failures for retry."""
    from .adapters import create_adapter
    from .database import SessionLocal
    from .delivery import deliver_pending
    from .accounting_status import refresh_due_accounting_statuses

    try:
        provider = create_adapter(adapter, directory=str(directory) if directory else None,
                                  endpoint=endpoint, token=token, company_id=company_id)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    with SessionLocal() as session:
        session.info["company_id"] = company_id
        summary = deliver_pending(
            session, provider, limit=limit,
            max_attempts=int(os.getenv("AP_RETRY_MAX_ATTEMPTS", "8")),
            initial_retry_seconds=int(os.getenv("AP_RETRY_INITIAL_SECONDS", "30")),
            max_retry_seconds=int(os.getenv("AP_RETRY_MAX_SECONDS", "3600")),
        )
    typer.echo(json.dumps(summary, indent=2))


@app.command("notifications-run")
def notifications_run(
    company_id: str = typer.Option("default", help="Company scope for this notification run."),
    window_hours: int = typer.Option(24, min=1, max=720,
                                     help="Notify reviewers this long before due time."),
    limit: int = typer.Option(100, min=1, max=1000,
                              help="Maximum notification outbox records to deliver."),
):
    """Escalate overdue items, enqueue due reminders, and deliver webhook notifications."""
    from .database import SessionLocal
    from .notification_delivery import (
        WebhookNotificationAdapter,
        deliver_pending_notifications,
        escalate_overdue,
        queue_due_reminders,
    )

    endpoint = os.getenv("NOTIFICATION_WEBHOOK_URL")
    if not endpoint:
        typer.echo("NOTIFICATION_WEBHOOK_URL is required", err=True)
        raise typer.Exit(code=1)
    try:
        adapter = WebhookNotificationAdapter(
            endpoint,
            token=os.getenv("NOTIFICATION_WEBHOOK_TOKEN"),
            secret=os.getenv("NOTIFICATION_WEBHOOK_SECRET"),
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    with SessionLocal() as session:
        session.info["company_id"] = company_id
        escalated = escalate_overdue(
            session, actor=os.getenv("NOTIFICATION_WORKER_NAME", "scheduler")
        )
        reminders = queue_due_reminders(session, window_hours=window_hours)
        session.commit()
        delivery = deliver_pending_notifications(
            session, adapter, limit=limit,
            max_attempts=int(os.getenv("NOTIFICATION_RETRY_MAX_ATTEMPTS", "8")),
            initial_retry_seconds=int(os.getenv("NOTIFICATION_RETRY_INITIAL_SECONDS", "30")),
            max_retry_seconds=int(os.getenv("NOTIFICATION_RETRY_MAX_SECONDS", "3600")),
        )
    typer.echo(json.dumps({
        "escalated_match_ids": escalated,
        "reminders_queued": len(reminders),
        "delivery": delivery,
    }, indent=2))
    if delivery["failed"] or delivery["dead_lettered"]:
        raise typer.Exit(code=1)


def parse_purchase_order_data(data: dict):
    from .schemas import PurchaseOrder

    if "purchase_order" in data:
        data = data["purchase_order"]
    return PurchaseOrder.model_validate(data)


@app.command()
def worker(
    interval: int = typer.Option(30, min=1, max=3600, help="Seconds between durable outbox polling runs."),
    limit: int = typer.Option(100, min=1, max=1000, help="Maximum records per outbox per company per cycle."),
    once: bool = typer.Option(False, help="Run one polling cycle and exit."),
):
    """Poll AP and notification outboxes with bounded retries and dead-letter handling."""
    import time
    from .adapters import create_adapter
    from .adapters.factory import create_adapter_for_company
    from sqlalchemy import select
    from datetime import datetime, timezone
    from .database import SessionLocal, WorkerHeartbeatRecord
    from .delivery import deliver_pending
    from .integration_auth import configured_company_ids
    from .notification_delivery import (
        WebhookNotificationAdapter, deliver_pending_notifications,
        escalate_overdue, queue_due_reminders,
    )

    adapter_name = os.getenv("AP_DELIVERY_ADAPTER", "filesystem")
    ap_attempts = int(os.getenv("AP_RETRY_MAX_ATTEMPTS", "8"))
    ap_initial = int(os.getenv("AP_RETRY_INITIAL_SECONDS", "30"))
    ap_max = int(os.getenv("AP_RETRY_MAX_SECONDS", "3600"))
    notification_attempts = int(os.getenv("NOTIFICATION_RETRY_MAX_ATTEMPTS", "8"))
    notification_initial = int(os.getenv("NOTIFICATION_RETRY_INITIAL_SECONDS", "30"))
    notification_max = int(os.getenv("NOTIFICATION_RETRY_MAX_SECONDS", "3600"))
    worker_name = os.getenv("DELIVERY_WORKER_NAME", "invoice-match-worker")
    notification_endpoint = os.getenv("NOTIFICATION_WEBHOOK_URL")
    notification_adapter = None
    if notification_endpoint:
        notification_adapter = WebhookNotificationAdapter(
            notification_endpoint, token=os.getenv("NOTIFICATION_WEBHOOK_TOKEN"),
            secret=os.getenv("NOTIFICATION_WEBHOOK_SECRET"),
        )
    companies = configured_company_ids()
    while True:
        cycle = []
        for company_id in companies:
            provider = create_adapter_for_company(company_id)
            with SessionLocal() as session:
                session.info["company_id"] = company_id
                ap = deliver_pending(session, provider, limit=limit, max_attempts=ap_attempts,
                                     initial_retry_seconds=ap_initial, max_retry_seconds=ap_max)
                accounting_status = refresh_due_accounting_statuses(
                    session, provider, limit=limit, actor=worker_name,
                )
                notifications = {"delivered": 0, "failed": 0, "dead_lettered": 0}
                if notification_adapter:
                    escalated = escalate_overdue(
                        session, actor=os.getenv("NOTIFICATION_WORKER_NAME", "worker")
                    )
                    reminders = queue_due_reminders(session)
                    session.commit()
                    notifications = deliver_pending_notifications(
                        session, notification_adapter, limit=limit,
                        max_attempts=notification_attempts,
                        initial_retry_seconds=notification_initial,
                        max_retry_seconds=notification_max,
                    )
                    notifications["escalated"] = len(escalated)
                    notifications["reminders_queued"] = len(reminders)
                now = datetime.now(timezone.utc)
                heartbeat = session.scalar(select(WorkerHeartbeatRecord).where(
                    WorkerHeartbeatRecord.worker_name == worker_name
                ))
                if heartbeat is None:
                    heartbeat = WorkerHeartbeatRecord(worker_name=worker_name,
                        poll_interval_seconds=interval, last_started_at=now)
                    session.add(heartbeat)
                heartbeat.poll_interval_seconds = interval
                heartbeat.last_started_at = now
                heartbeat.last_succeeded_at = now
                failures = ap.get("failed", 0) + ap.get("dead_lettered", 0) \
                    + notifications.get("failed", 0) + notifications.get("dead_lettered", 0) \
                    + accounting_status.get("failed", 0)
                heartbeat.last_error = f"{failures} delivery attempt(s) failed or dead-lettered" if failures else None
                session.commit()
                cycle.append({"company_id": company_id, "ap": ap,
                              "accounting_status": accounting_status, "notifications": notifications,
                              "health": "DEGRADED" if failures else "HEALTHY"})
        typer.echo(json.dumps({"companies": cycle}, indent=2))
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    app()
