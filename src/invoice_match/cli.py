from __future__ import annotations

import json
import os
from pathlib import Path

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


@app.command()
def deliver(
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

    try:
        provider = create_adapter(adapter, directory=str(directory) if directory else None,
                                  endpoint=endpoint, token=token)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    with SessionLocal() as session:
        summary = deliver_pending(session, provider, limit=limit)
    typer.echo(json.dumps(summary, indent=2))


@app.command("notifications-run")
def notifications_run(
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
        escalated = escalate_overdue(
            session, actor=os.getenv("NOTIFICATION_WORKER_NAME", "scheduler")
        )
        reminders = queue_due_reminders(session, window_hours=window_hours)
        session.commit()
        delivery = deliver_pending_notifications(session, adapter, limit=limit)
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


if __name__ == "__main__":
    app()
