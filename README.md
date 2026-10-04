# invoice-match

**A lightweight, self-hosted three-way invoice matching engine for SMB finance teams.**

`invoice-match` compares a purchase order (PO), goods receipt, and supplier invoice. It identifies quantity, price, vendor, currency, and tax discrepancies, places exceptions in a review queue, and records approval decisions in an audit trail.

Documents enter as validated JSON. Connect an ERP, document management system, or OCR service upstream and map its output to the schemas in this project.

## Features

- **Three-way matching:** Compare PO, receipt, and invoice lines by SKU, quantity, and unit price.
- **Financial terms:** Check line discounts, tax codes and rates, invoice tax, freight, document discounts, totals, and currency conversion.
- **Currency precision:** Configure conversion rates and minor-unit precision for currencies such as JPY and KWD.
- **Rule-driven approvals:** Select approval policies by amount, variance, supplier, or cost center, with role, assignment, approval count, and SLA.
- **Exception queue:** List, approve, and reject discrepancies through the API.
- **Authenticated integrations:** Protect matching, CSV intake, and AP export routes with service bearer tokens.
- **Idempotency and duplicate detection:** Replay safe request retries and reject duplicate supplier invoices.
- **AP export outbox:** Poll approved payables as structured JSON and acknowledge downstream delivery.
- **CSV intake:** Submit line-oriented PO, receipt, and invoice CSVs through the same matching pipeline.
- **Reviewer roles:** Separate read-only reviewers, approvers, and admins; support distinct reviewers for multi-step approval.
- **Audit history:** Record integration and reviewer identities, approval votes, decisions, and AP acknowledgements.
- **Database migrations:** Evolve PostgreSQL and SQLite schemas with Alembic revisions.
- **Persistent storage:** Use PostgreSQL in Docker Compose or SQLite for local development.
- **Two interfaces:** Run one-off matches from the CLI or submit matches to the FastAPI service.
- **Continuous integration:** Run the matching and reviewer workflow suite on pushes and pull requests.

## How matching works

For each SKU, the engine checks that invoice quantities do not exceed received quantities or the PO quantity beyond the configured tolerance. Partial receipts and partial invoices are allowed. A receipt quantity below the PO quantity is not itself an exception.

The engine also compares net unit prices after discounts and currency conversion. It checks tax codes/rates, tax amounts when enough PO tax information exists, freight and document discounts against the PO, and reported invoice totals against line, tax, freight, and discount arithmetic. It does not guess missing tax rates. Monetary arithmetic uses configured minor-unit precision and `ROUND_HALF_UP`.

When a document contains the same SKU on multiple rows, quantities are combined and unit prices are compared using the weighted average price for those rows. The matcher returns a primary reason at the top level and includes all detected issues in `discrepancies`.

## Requirements

- Python 3.11 or newer for local use
- Docker and Docker Compose for the containerized PostgreSQL setup

## Quick start: CLI

Clone the repository and install it in an isolated Python environment (replace `YOUR-ORG` with the GitHub account or organization hosting the repository):

```bash
git clone https://github.com/YOUR-ORG/automated_invoice_reconciliation.git
cd automated_invoice_reconciliation
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

Run the included example:

```bash
invoice-match match \
  --po examples/po.json \
  --receipt examples/receipt.json \
  --invoice examples/invoice.json
```

The example has 100 units on the PO and invoice, but only 95 received. The command prints a JSON `EXCEPTION` with reason `RECEIPT_QUANTITY_MISMATCH`, expected quantity 95, invoiced quantity 100, and variance 5. It exits with status `2` for an exception and `0` for a match. This makes the CLI suitable for scripts that branch on the result.

Use another rule file with `--rules`:

```bash
invoice-match match \
  --po examples/po.json \
  --receipt examples/receipt.json \
  --invoice examples/invoice.json \
  --rules config/rules.yaml
```

See available commands and options:

```bash
invoice-match --help
invoice-match match --help
```

## Run the API locally

For local use, the API stores data in SQLite. Configure reviewer and integration identities, then apply database migrations before starting the API:

```bash
export DATABASE_URL='sqlite:///./invoice_match.db'
export REVIEWER_TOKENS='{"finance-reviewer":{"token":"replace-with-a-random-reviewer-token-at-least-32-chars","roles":["reviewer"]},"finance-manager":{"token":"replace-with-a-different-approver-token-at-least-32-chars","roles":["reviewer","approver"]},"finance-admin":{"token":"replace-with-a-different-admin-token-at-least-32-chars","roles":["reviewer","approver","admin"]}}'
export INTEGRATION_TOKENS='{"ap-import":"replace-with-a-random-integration-token-at-least-32-chars"}'
alembic upgrade head
invoice-match serve
```

Reviewer routes fail closed with HTTP `503` when `REVIEWER_TOKENS` is absent or invalid. Each reviewer needs a unique bearer token (at least 32 characters) and roles from `reviewer`, `approver`, and `admin`. Integration routes similarly require `INTEGRATION_TOKENS`, a JSON map of service names to unique tokens at least 32 characters long. Keep real tokens in a secret manager or local environment file; never commit them.

The service listens on `http://127.0.0.1:8000`. Open [the interactive Swagger UI](http://127.0.0.1:8000/docs) or [the ReDoc API reference](http://127.0.0.1:8000/redoc).

To bind a different host or port:

```bash
invoice-match serve --host 0.0.0.0 --port 8080
```

### Submit a match

The API accepts the same three documents in a single request. The combined example is [examples/match-request.json](examples/match-request.json).

```bash
curl --request POST http://127.0.0.1:8000/matches \
  --header 'Content-Type: application/json' \
  --header 'Authorization: Bearer replace-with-a-random-integration-token-at-least-32-chars' \
  --header 'Idempotency-Key: erp-invoice-INV-2044-v1' \
  --data-binary @examples/match-request.json
```

The integration identity is recorded in the audit log; the client cannot supply an actor name. Idempotency keys are required (8–128 characters). Repeating the same key and request returns the original match with HTTP `200`; reusing the key with different content returns `409`. A repeated supplier/invoice number returns `409 DUPLICATE_SUPPLIER_INVOICE`, even with a new key. Exceptions enter the review queue automatically.

### Import CSV documents

`POST /imports/csv` accepts three CSV strings in JSON and uses the same integration bearer token and idempotency header. Each CSV has one document per request and one row per line item. Required columns are `number,vendor,sku,qty` for PO; `sku,qty` for receipt; and `number,sku,qty` for invoice. Unit `price` is optional, as it is in the JSON schema. Optional columns map to the input schema: `currency`, `cost_center`, `tax_rate`, `tax_code`, `tax_amount`, `discount_rate`, `freight_amount`, `discount_amount`, `total_amount`, and `description`. Document-level values may appear on the first row or repeat consistently on every row. Invalid data returns HTTP `422`.

The JSON body has string fields `po_csv`, `receipt_csv`, and `invoice_csv`. Start with [examples/csv-import.json](examples/csv-import.json), or see the `CsvMatchRequest` schema in `/docs`.

### Review and decide an exception

Reviewer routes require an `Authorization: Bearer <token>` header. The example below uses the `finance-reviewer` token configured above. List open exceptions, oldest first:

```bash
curl http://127.0.0.1:8000/exceptions \
  --header 'Authorization: Bearer replace-with-a-long-random-secret'
```

Get all exceptions, including closed items:

```bash
curl 'http://127.0.0.1:8000/exceptions?status=ALL' \
  --header 'Authorization: Bearer replace-with-a-long-random-secret'
```

Approve or reject a pending exception using its `match_id`:

```bash
curl --request POST http://127.0.0.1:8000/exceptions/MATCH_ID/approve \
  --header 'Authorization: Bearer replace-with-a-long-random-secret' \
  --header 'Content-Type: application/json' \
  --data '{"comment":"Approved against supplier confirmation."}'
```

```bash
curl --request POST http://127.0.0.1:8000/exceptions/MATCH_ID/reject \
  --header 'Authorization: Bearer replace-with-a-long-random-secret' \
  --header 'Content-Type: application/json' \
  --data '{"comment":"Please request a corrected invoice."}'
```

Retrieve the decision history for a match:

```bash
curl http://127.0.0.1:8000/matches/MATCH_ID/audit \
  --header 'Authorization: Bearer replace-with-a-long-random-secret'
```

Approvers and admins may decide exceptions; a `reviewer` role is read-only. A policy requiring `admin` rejects approver decisions, and assigned items can only be decided by the named reviewer or an admin. Multi-step policies require distinct reviewers; partial approval leaves the item pending and open. Audit identity comes from the token, never the request body. Repeated votes return HTTP `409`; unknown matches return `404`; missing or invalid credentials return `401`.

### Deliver approved payables to an AP system

Poll pending entries, persist each payload to the accounting system, then acknowledge the match ID after the downstream system confirms delivery:

```bash
curl 'http://127.0.0.1:8000/integrations/ap/outbox?limit=100' \
  --header 'Authorization: Bearer replace-with-a-random-integration-token-at-least-32-chars'

curl --request POST http://127.0.0.1:8000/integrations/ap/outbox/MATCH_ID/ack \
  --header 'Authorization: Bearer replace-with-a-random-integration-token-at-least-32-chars'
```

Each payload contains invoice and PO identifiers, supplier, currency, cost center, lines, subtotal, tax, freight, discounts, total, and approval status. Delivery is at-least-once; consumers should deduplicate by `match_id` and acknowledge only after a successful AP write.

## API reference

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health check |
| `POST` | `/matches` | Match and persist documents (integration token and idempotency key required) |
| `POST` | `/imports/csv` | Parse CSV documents and submit a match (integration token and idempotency key required) |
| `GET` | `/matches/{match_id}` | Retrieve a stored match result (reviewer bearer token required) |
| `GET` | `/matches/{match_id}/audit` | Retrieve audit events (reviewer bearer token required) |
| `GET` | `/exceptions?status=OPEN\|CLOSED\|ALL` | List exceptions; defaults to `OPEN` (reviewer bearer token required) |
| `POST` | `/exceptions/{match_id}/approve` | Record an approval vote (approver/admin token required) |
| `POST` | `/exceptions/{match_id}/reject` | Reject an exception (approver/admin token required) |
| `GET` | `/integrations/ap/outbox` | Poll pending AP exports (integration token required) |
| `POST` | `/integrations/ap/outbox/{match_id}/ack` | Acknowledge delivery (integration token required) |

Interactive API documentation is available at `/docs` and `/redoc` while the service is running.

## Input format

Each line item requires an SKU and a positive quantity. PO and invoice line items also accept a unit `price`. The full sample documents are [PO](examples/po.json), [receipt](examples/receipt.json), and [invoice](examples/invoice.json).

```json
{
  "po": {
    "number": "PO-10291",
    "vendor": "ABC Supplies",
    "currency": "USD",
    "items": [{"sku": "A100", "qty": 100, "price": 10}]
  },
  "receipt": {
    "items": [{"sku": "A100", "qty": 95}]
  },
  "invoice": {
    "number": "INV-2044",
    "vendor": "ABC Supplies",
    "currency": "USD",
    "items": [{"sku": "A100", "qty": 100, "price": 10}]
  }
}
```

Document fields:

- `po.number` and `po.vendor` identify the PO and supplier.
- `po.items[]` contains `sku`, `qty`, and optionally `price` per unit.
- `receipt.items[]` contains received `sku` and `qty` values. A receipt number is optional.
- `invoice.items[]` contains invoiced `sku`, `qty`, and optionally `price` per unit.
- PO and invoice `currency` default to `USD` and use three-letter currency codes.
- Optional `po.tax_rate` is a fraction: `0.08` means 8%.
- Optional `invoice.tax_amount` is the tax amount stated on the invoice.

Pydantic validates the input and rejects unknown fields. Decimal arithmetic is used for quantities and prices.

## Configure matching rules

The default rules are in [config/rules.yaml](config/rules.yaml):

```yaml
rules:
  quantity_variance:
    tolerance: 2%
  price_variance:
    tolerance: 1%
  tax_variance:
    tolerance: 0.5%
  amount_variance:
    tolerance: 0.5%
  currency:
    base_currency: USD
    minor_units:
      USD: 2
      JPY: 0
    rates_to_base:
      USD: 1
      EUR: 1.08
  approval:
    auto_approve_matches: false
    policies:
      - name: high-value-two-step
        min_invoice_total: 10000
        required_role: admin
        approvals_required: 2
        sla_hours: 24
      - name: default
        required_role: approver
        approvals_required: 1
        sla_hours: 72
```

Tolerance is relative to the expected value: `2%` accepts variance up to 2% of expected. A numeric ratio such as `0.02` is also accepted. Currency rates convert one unit of the named currency into units of the base currency; set the base currency rate to `1`. Both document currencies must have rates. `minor_units` controls rounding per currency (defaults to 2). Policies are checked in order and the first matching policy wins; selectors include invoice total, accumulated monetary variance, supplier, and cost center. Monetary selectors use the base currency. `sla_hours` sets an exception's due timestamp.

Rates are static configuration values supplied by the operator. This application does not fetch exchange rates. Set `INVOICE_MATCH_RULES` to load another YAML file for the API or CLI default; the CLI `--rules` option overrides it.

## Run with Docker Compose

Set reviewer credentials, then start the API and PostgreSQL with a persistent database volume. The Compose startup command applies pending migrations before serving requests.

```bash
cp .env.example .env
# Set unique random reviewer and integration tokens in .env.
docker compose up --build
```

The API is available at `http://localhost:8000`; PostgreSQL is exposed on port `5432`. OpenAPI docs are at `http://localhost:8000/docs`. Stop the services with `Ctrl+C`, or run `docker compose down`. The database volume is retained; remove it with `docker compose down --volumes` if you intentionally want to delete local data.

The Compose file uses development credentials. Change credentials and manage them as deployment secrets before using this setup in a shared or production environment.

## Configuration and storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./invoice_match.db` | SQLAlchemy database URL |
| `INVOICE_MATCH_RULES` | `config/rules.yaml` | YAML rules path |
| `REVIEWER_TOKENS` | unset | JSON map of reviewer names to `{token, roles}`; roles: reviewer, approver, admin |
| `INTEGRATION_TOKENS` | unset | JSON map of integration names to unique bearer tokens |

Example PostgreSQL URL:

```bash
export DATABASE_URL='postgresql+psycopg://invoice_match:your-password@localhost:5432/invoice_match'
export REVIEWER_TOKENS='{"finance-manager":{"token":"replace-with-a-random-approver-token-at-least-32-chars","roles":["reviewer","approver"]}}'
export INTEGRATION_TOKENS='{"ap-import":"replace-with-a-random-integration-token-at-least-32-chars"}'
alembic upgrade head
invoice-match serve
```

Schema changes are managed with Alembic. Run `alembic upgrade head` before starting the API; Docker Compose applies pending migrations. Inspect state with `alembic current` and `alembic history`. If upgrading a database created by an earlier release using `create_all`, stop the API, run `alembic stamp 0001_initial` once, then run `alembic upgrade head`. Match inputs and results are in `matches`, review items in `exceptions`, events in `audit_events`, and downstream delivery state in `ap_outbox`.

## Project layout

```text
config/rules.yaml             Matching tolerances and currency rates
examples/                     Sample PO, receipt, and invoice JSON
src/invoice_match/api.py      Authenticated API, intake, review, and outbox routes
src/invoice_match/ap_export.py AP payload builder for approved matches
src/invoice_match/csv_import.py Provider-neutral CSV document parser
src/invoice_match/cli.py      Typer CLI
src/invoice_match/database.py SQLAlchemy models and database setup
src/invoice_match/matcher.py  Three-way matching rules
src/invoice_match/parsers.py  JSON document parsers
src/invoice_match/rules.py    YAML rule loading and validation
src/invoice_match/schemas.py  Pydantic request and response schemas
migrations/                   Alembic environment and schema revisions
tests/                        Matching and reviewer workflow tests
```

## Development

Install the package in editable mode and inspect the available commands:

```bash
python -m pip install -e '.[dev]'
pytest
invoice-match --help
```

The automated suite covers matching and financial calculations, idempotent/duplicate submission behavior, CSV validation, role-based approval workflows, the AP outbox, migrations, and audit identity.

## Security and scope

Reviewer read, audit, and queue routes require reviewer tokens; approve and reject routes additionally require the `approver` or `admin` role. Integration submission and AP outbox routes require tokens from `INTEGRATION_TOKENS`; `/health` is public. Store tokens in a secret manager in deployed environments and rotate them when access changes. The example PostgreSQL credentials in Docker Compose are for local development only.

This project validates and matches structured JSON and CSV, and offers a provider-neutral AP outbox. It does not perform OCR, include vendor-specific ERP/AP connectors, initiate payments, or fetch live exchange rates. Build an adapter for your accounting system using the outbox contract. Review tolerances, tax assumptions, and rates against your accounting policy before processing live invoices.

## Inspiration

The README structure follows the practical, task-oriented documentation style used by popular Python projects such as [FastAPI](https://github.com/fastapi/fastapi) and [Typer](https://github.com/fastapi/typer): install, run a working example, then explore the API and configuration.

## License

Distributed under the [MIT License](LICENSE).
