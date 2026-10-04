# invoice-match

**A lightweight, self-hosted three-way invoice matching engine for SMB finance teams.**

`invoice-match` compares a purchase order (PO), goods receipt, and supplier invoice. It identifies quantity, price, vendor, currency, and tax discrepancies, places exceptions in a review queue, and records approval decisions in an audit trail.

Documents enter as validated JSON. Connect an ERP, document management system, or OCR service upstream and map its output to the schemas in this project.

## Features

- **Three-way matching:** Compare PO, receipt, and invoice lines by SKU, quantity, and unit price.
- **Tolerance rules:** Tune quantity, price, and tax variances in YAML.
- **Currency conversion:** Compare document prices in a configured base currency using operator-supplied rates.
- **Exception queue:** List, approve, and reject discrepancies through the API.
- **Reviewer authentication:** Protect exception, match-read, and audit routes with named bearer-token credentials.
- **Audit history:** Record match creation and approval decisions with authenticated reviewer identity and comment details.
- **Database migrations:** Evolve PostgreSQL and SQLite schemas with Alembic revisions.
- **Persistent storage:** Use PostgreSQL in Docker Compose or SQLite for local development.
- **Two interfaces:** Run one-off matches from the CLI or submit matches to the FastAPI service.
- **Continuous integration:** Run the matching and reviewer workflow suite on pushes and pull requests.

## How matching works

For each SKU, the engine checks that invoice quantities do not exceed received quantities or the PO quantity beyond the configured tolerance. Partial receipts and partial invoices are allowed. A receipt quantity below the PO quantity is not itself an exception.

The engine also compares available PO and invoice unit prices after currency conversion. If a PO tax rate and invoice tax amount are both present, it checks invoice tax against that rate. It does not infer a tax rate when one is missing.

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

For local use, the API stores data in SQLite. Configure at least one named reviewer token and apply the initial database migration before starting the API:

```bash
export DATABASE_URL='sqlite:///./invoice_match.db'
export REVIEWER_TOKENS='{"finance-reviewer":"replace-with-a-long-random-secret"}'
alembic upgrade head
invoice-match serve
```

Reviewer routes fail closed with HTTP `503` when `REVIEWER_TOKENS` is not configured or contains invalid credentials. The value is a JSON object mapping reviewer names to unique bearer tokens of at least 32 characters. Keep real tokens in a secret manager or local environment file, and do not commit them.

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
  --header 'X-Actor: ap-import' \
  --data-binary @examples/match-request.json
```

The API returns a match ID, status, discrepancies, and approval status. An exception is saved to the queue automatically.

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

Audit actor identity comes from the matched bearer token's configured reviewer name; clients cannot choose or spoof the reviewer name in the decision body. An exception can only be decided once. A repeated decision returns HTTP `409`; an unknown match ID returns HTTP `404`; missing or invalid credentials return HTTP `401`.

## API reference

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Service health check |
| `POST` | `/matches` | Match and persist PO, receipt, and invoice documents |
| `GET` | `/matches/{match_id}` | Retrieve a stored match result (reviewer bearer token required) |
| `GET` | `/matches/{match_id}/audit` | Retrieve audit events (reviewer bearer token required) |
| `GET` | `/exceptions?status=OPEN\|CLOSED\|ALL` | List exceptions; defaults to `OPEN` (reviewer bearer token required) |
| `POST` | `/exceptions/{match_id}/approve` | Approve a pending exception (reviewer bearer token required) |
| `POST` | `/exceptions/{match_id}/reject` | Reject a pending exception (reviewer bearer token required) |

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
  currency:
    base_currency: USD
    rates_to_base:
      USD: 1
      EUR: 1.08
  approval:
    auto_approve_matches: false
```

Tolerance is relative to the expected value: `2%` accepts variance up to 2% of expected. A numeric ratio such as `0.02` is also accepted. Currency rates convert one unit of the named currency into units of the base currency; set the base currency rate to `1`. Both the PO and invoice currencies must have rates. If either rate is missing, the match returns a `CURRENCY_MISMATCH` exception.

Rates are static configuration values supplied by the operator. This application does not fetch exchange rates. Set `INVOICE_MATCH_RULES` to load another YAML file for the API or CLI default; the CLI `--rules` option overrides it.

## Run with Docker Compose

Set reviewer credentials, then start the API and PostgreSQL with a persistent database volume. The Compose startup command applies pending migrations before serving requests.

```bash
cp .env.example .env
# Edit REVIEWER_TOKENS in .env and replace the example token.
docker compose up --build
```

The API is available at `http://localhost:8000`; PostgreSQL is exposed on port `5432`. OpenAPI docs are at `http://localhost:8000/docs`. Stop the services with `Ctrl+C`, or run `docker compose down`. The database volume is retained; remove it with `docker compose down --volumes` if you intentionally want to delete local data.

The Compose file uses development credentials. Change credentials and manage them as deployment secrets before using this setup in a shared or production environment.

## Configuration and storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./invoice_match.db` | SQLAlchemy database URL |
| `INVOICE_MATCH_RULES` | `config/rules.yaml` | YAML rules path |
| `REVIEWER_TOKENS` | unset | JSON object mapping reviewer names to bearer tokens |

Example PostgreSQL URL:

```bash
export DATABASE_URL='postgresql+psycopg://invoice_match:your-password@localhost:5432/invoice_match'
export REVIEWER_TOKENS='{"finance-reviewer":"replace-with-a-long-random-secret"}'
alembic upgrade head
invoice-match serve
```

Schema changes are managed with Alembic. Run `alembic upgrade head` after installing a new version and before starting the API; Docker Compose does this at container start. To inspect migration state or generate a migration during development, use `alembic current`, `alembic history`, and `alembic revision --autogenerate -m "describe schema change"`, then review and edit generated migration scripts before applying them. If upgrading a database created by an earlier version of this project that used `create_all`, stop the API and run `alembic stamp 0001_initial` once to mark the existing schema as the initial revision; then use `alembic upgrade head` normally. Match inputs and results are stored in `matches`; open and closed review items in `exceptions`; and submission and decision events in `audit_events`.

## Project layout

```text
config/rules.yaml             Matching tolerances and currency rates
examples/                     Sample PO, receipt, and invoice JSON
src/invoice_match/api.py      FastAPI endpoints and approval workflow
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

The automated suite covers matching decisions, tolerance boundaries, currency conversion, tax checks, and authenticated approval/rejection behavior including audit identity and one-time decisions.

## Security and scope

Reviewer list, read, audit, approve, and reject routes use bearer tokens configured by reviewer name in `REVIEWER_TOKENS`. Every configured reviewer has the same read, approve, and reject permissions; per-role authorization is not implemented. Store tokens in a secret manager in deployed environments and rotate them when access changes. `POST /matches` and `/health` remain unauthenticated integration endpoints; restrict network access to match submission and place the API behind an authenticated gateway or service network before using real financial records. The example PostgreSQL credentials in Docker Compose are for local development only.

This project validates and matches structured JSON. It does not perform OCR, connect to ERP/AP systems, initiate payments, or fetch live exchange rates. Tax is checked only when both the PO tax rate and invoice tax amount are supplied. Review tolerances, tax assumptions, and rates against your accounting policy before processing live invoices.

## Inspiration

The README structure follows the practical, task-oriented documentation style used by popular Python projects such as [FastAPI](https://github.com/fastapi/fastapi) and [Typer](https://github.com/fastapi/typer): install, run a working example, then explore the API and configuration.

## License

Distributed under the [MIT License](LICENSE).
