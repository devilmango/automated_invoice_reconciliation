from __future__ import annotations

import json
from pathlib import Path

import typer

from .matcher import match_documents
from .parsers import parse_goods_receipt, parse_purchase_order, parse_supplier_invoice
from .rules import load_rules
from .schemas import MatchRequest

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


if __name__ == "__main__":
    app()
