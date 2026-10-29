"""Small, dependency-free parsers for common bank statement exports."""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO


def _date(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y%m%d%H%M%S", "%y%m%d"):
        try:
            return datetime.strptime(value[:len(datetime.now().strftime(fmt))], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid bank transaction date: {value!r}") from exc


def _transaction(external_id: str, booked_at: str, amount: Decimal, *, vendor: str,
                 reference: str | None, currency: str, description: str = "") -> dict:
    if not external_id.strip():
        raise ValueError("Each bank transaction requires a stable transaction ID")
    if not amount.is_finite() or amount == 0:
        raise ValueError(f"Bank transaction {external_id!r} must have a non-zero finite amount")
    return {"external_id": external_id.strip(), "payment_date": _date(booked_at),
            "amount": abs(amount), "direction": "DEBIT" if amount < 0 else "CREDIT",
            "vendor": (vendor or "Unknown payee").strip()[:255],
            "reference": (reference or None), "currency": (currency or "USD").strip().upper(),
            "description": description[:1000]}


def parse_csv(content: str) -> list[dict]:
    reader = csv.DictReader(StringIO(content))
    if not reader.fieldnames:
        raise ValueError("CSV statement must include a header row")
    rows = []
    for line, raw in enumerate(reader, start=2):
        row = {re.sub(r"[^a-z0-9]", "", (key or "").casefold()): (value or "").strip()
               for key, value in raw.items()}
        try:
            txid = row.get("transactionid") or row.get("fitid") or row.get("id")
            date = row.get("date") or row.get("txndate") or row.get("bookingdate")
            if row.get("amount"):
                amount = Decimal(row["amount"].replace(",", ""))
            else:
                debit, credit = Decimal(row.get("debit") or 0), Decimal(row.get("credit") or 0)
                if debit and credit:
                    raise ValueError("Provide either amount or one of debit/credit")
                amount = credit - debit
            rows.append(_transaction(txid or "", date or "", amount,
                vendor=row.get("vendor") or row.get("payee") or row.get("name") or "",
                reference=row.get("reference") or row.get("memo"),
                currency=row.get("currency") or "USD", description=row.get("description") or ""))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid CSV statement row {line}: {exc}") from exc
    return rows


def parse_ofx(content: str) -> list[dict]:
    rows = []
    statement_currency = re.search(r"<CURDEF>([^<\\r\\n]+)", content, flags=re.IGNORECASE)
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>|</BANKTRANLIST>))", content,
                           flags=re.IGNORECASE | re.DOTALL):
        fields = {key.upper(): value.strip() for key, value in re.findall(
            r"<([A-Z0-9]+)>([^<\r\n]+)", block, flags=re.IGNORECASE)}
        try:
            rows.append(_transaction(fields.get("FITID", ""), fields.get("DTPOSTED", ""),
                Decimal(fields.get("TRNAMT", "")), vendor=fields.get("NAME") or "",
                reference=fields.get("CHECKNUM") or fields.get("REFNUM") or fields.get("MEMO"),
                currency=fields.get("CURDEF") or (statement_currency.group(1).strip() if statement_currency else "USD"),
                description=fields.get("MEMO") or ""))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid OFX transaction: {exc}") from exc
    if not rows:
        raise ValueError("No OFX statement transactions found")
    return rows


def parse_bai2(content: str, transaction_codes: dict[str, str] | None = None) -> list[dict]:
    """Parse BAI2 03/16 records; amounts use BAI's unsigned cents convention.

    Transaction codes 400-699 are treated as debits and the remaining detail codes
    as credits. BAI2 installations can customize codes, so callers should review the
    imported queue before allocating any transaction.
    """
    date, currency = None, "USD"
    rows = []
    for raw in content.splitlines():
        fields = [part.strip() for part in raw.strip().rstrip("/").split(",")]
        if not fields:
            continue
        if fields[0] == "02" and len(fields) >= 5:
            date = fields[4]
            currency = fields[6] if len(fields) > 6 and fields[6] else "USD"
        elif fields[0] == "03" and len(fields) >= 3:
            currency = fields[2] or currency
        elif fields[0] == "16" and len(fields) >= 3:
            if date is None:
                raise ValueError("BAI2 account summary (03) must precede transaction (16) records")
            try:
                code, amount_minor = int(fields[1]), Decimal(fields[2])
                zero_decimal = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
                three_decimal = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
                places = 0 if currency in zero_decimal else 3 if currency in three_decimal else 2
                amount = amount_minor / (Decimal(10) ** places)
                configured_direction = (transaction_codes or {}).get(str(code))
                if configured_direction == "DEBIT":
                    signed = -amount
                elif configured_direction in {"CREDIT", "SKIP"}:
                    signed = amount
                else:
                    signed = -amount if 400 <= code <= 699 else amount
                bank_ref = fields[4] if len(fields) > 4 else ""
                customer_ref = fields[5] if len(fields) > 5 else ""
                text = fields[6] if len(fields) > 6 else ""
                txid = bank_ref or customer_ref
                row = _transaction(txid, date, signed, vendor=text or "Unknown payee",
                    reference=customer_ref or None, currency=currency, description=text)
                if configured_direction == "SKIP":
                    row["direction"] = "SKIP"
                rows.append(row)
            except (ValueError, InvalidOperation) as exc:
                raise ValueError(f"Invalid BAI2 transaction record: {exc}") from exc
    if not rows:
        raise ValueError("No supported BAI2 transaction (16) records found")
    return rows


def parse_statement(format: str, content: str, *, bai2_transaction_codes: dict[str, str] | None = None) -> list[dict]:
    if not content.strip():
        raise ValueError("Statement content cannot be empty")
    parsers = {"csv": parse_csv, "ofx": parse_ofx,
               "bai2": lambda value: parse_bai2(value, bai2_transaction_codes)}
    try:
        return parsers[format.casefold()](content)
    except KeyError as exc:
        raise ValueError("format must be csv, ofx, or bai2") from exc
