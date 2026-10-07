from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from email import message_from_bytes
from email.policy import default
from io import BytesIO
from pathlib import Path

_LABELS = {
    "number": re.compile(r"^(?:supplier\s+)?invoice\s*(?:number|no\.?|#)\s*[:#-]?\s*(.+)$", re.I),
    "vendor": re.compile(r"^(?:supplier|vendor|sold\s+by)\s*[:#-]\s*(.+)$", re.I),
    "currency": re.compile(r"^(?:currency)\s*[:#-]\s*([A-Z]{3})\b", re.I),
    "tax_amount": re.compile(r"^(?:tax|vat|gst)(?:\s+amount)?\s*[:#-]\s*[^\d-]*([\d,]+(?:\.\d+)?)\s*$", re.I),
    "total_amount": re.compile(r"^(?:(?:grand\s+)?total|amount\s+due|balance\s+due)\s*[:#-]\s*[^\d-]*([\d,]+(?:\.\d+)?)\s*$", re.I),
}
_ITEM = re.compile(
    r"^ITEM\s*:\s*([A-Z0-9._-]+)\s*[,|]\s*(?:QTY\s*=\s*)?([\d.]+)"
    r"\s*[,|]\s*(?:PRICE\s*=\s*)?[^\d-]*([\d,]+(?:\.\d+)?)\s*$",
    re.I,
)
_TABLE_ITEM = re.compile(r"^\s*([A-Z][A-Z0-9._-]{1,30})\s+([\d.]+)\s+([\d,]+\.\d{2})\s*$", re.I)


def _ocr_pdf(data: bytes, page_count: int) -> str:
    """Run optional local OCR on a bounded number of pages; never invoke a shell."""
    renderer = shutil.which(os.getenv("INVOICE_MATCH_PDF_RENDERER", "pdftoppm"))
    ocr = shutil.which(os.getenv("INVOICE_MATCH_OCR_ENGINE", "tesseract"))
    if not renderer or not ocr:
        return ""
    try:
        max_pages = max(1, min(int(os.getenv("INVOICE_MATCH_OCR_MAX_PAGES", "20")), 20))
    except ValueError:
        max_pages = 20
    if page_count > max_pages:
        return ""
    with tempfile.TemporaryDirectory(prefix="invoice-match-ocr-") as temp_dir:
        root = Path(temp_dir)
        pdf_path = root / "source.pdf"
        prefix = root / "page"
        pdf_path.write_bytes(data)
        try:
            subprocess.run(
                [
                    renderer, "-f", "1", "-l", str(page_count), "-r", "200", "-png",
                    str(pdf_path), str(prefix),
                ],
                check=True, capture_output=True, timeout=45,
            )
            pages = sorted(root.glob("page-*.png"))
            results = []
            for image in pages:
                result = subprocess.run(
                    [ocr, str(image), "stdout", "-l", os.getenv("INVOICE_MATCH_OCR_LANGUAGE", "eng")],
                    check=True, capture_output=True, timeout=30,
                )
                results.append(result.stdout.decode("utf-8", errors="replace"))
            return "\n".join(results)
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""


def _pdf_text(data: bytes) -> tuple[str, bool, str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires pypdf; install the project dependencies") from exc
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs cannot be extracted")
        if len(reader.pages) > 100:
            raise ValueError("PDF exceeds the 100-page extraction limit")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if len(text.strip()) >= 30:
            return text, False, ""
        if os.getenv("INVOICE_MATCH_OCR_ENABLED", "true").casefold() in {"0", "false", "no", "off"}:
            return text, False, "OCR_DISABLED"
        ocr_text = _ocr_pdf(data, len(reader.pages))
        if ocr_text.strip():
            return ocr_text, True, "OCR_USED_REVIEW_REQUIRED"
        return text, False, "OCR_UNAVAILABLE_OR_NO_TEXT_RECOGNIZED"
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read PDF document: {exc}") from exc


def _extract_fields(text: str, email_vendor: str | None = None) -> tuple[dict, dict[str, float], list[str]]:
    fields: dict = {"number": None, "vendor": email_vendor, "currency": None, "items": []}
    confidence: dict[str, float] = {}
    notes: list[str] = []
    for line in text.splitlines():
        candidate = line.strip()
        for key, pattern in _LABELS.items():
            match = pattern.match(candidate)
            if match:
                value = match.group(1).strip()
                if key.endswith("amount"):
                    fields[key] = value.replace(",", "")
                elif key == "currency":
                    fields[key] = value.upper()
                else:
                    fields[key] = value
                confidence[key] = 0.98
                break
        item_match = _ITEM.match(candidate)
        if item_match:
            sku, quantity, price = item_match.groups()
            fields["items"].append({"sku": sku, "qty": quantity, "price": price.replace(",", "")})
            continue
        table_match = _TABLE_ITEM.match(candidate)
        if table_match:
            sku, quantity, price = table_match.groups()
            fields["items"].append({"sku": sku, "qty": quantity, "price": price.replace(",", "")})

    if fields["vendor"]:
        confidence.setdefault("vendor", 0.82 if email_vendor else 0.96)
    if fields["items"]:
        confidence["items"] = 0.78 if any(_ITEM.match(line.strip()) for line in text.splitlines()) else 0.68
    if not fields["currency"]:
        currency = re.search(r"\b(USD|EUR|GBP|CAD|AUD|JPY|INR)\b", text, re.I)
        symbol = re.search(r"[$€£]", text)
        if currency:
            fields["currency"] = currency.group(1).upper()
            confidence["currency"] = 0.75
        elif symbol:
            fields["currency"] = {"$": "USD", "€": "EUR", "£": "GBP"}[symbol.group(0)]
            confidence["currency"] = 0.55
    if "number" not in confidence:
        notes.append("INVOICE_NUMBER_NOT_FOUND")
    if not fields["vendor"]:
        notes.append("VENDOR_NOT_FOUND")
    if not fields["items"]:
        notes.append("LINE_ITEMS_NOT_FOUND")
    if "total_amount" not in fields:
        notes.append("TOTAL_NOT_FOUND")
    if "currency" not in confidence:
        notes.append("CURRENCY_NOT_FOUND")
    low_confidence = sorted(key for key, value in confidence.items() if value < 0.8)
    if low_confidence:
        notes.append("LOW_CONFIDENCE_FIELDS:" + ",".join(low_confidence))
    if not text.strip():
        notes.append("NO_TEXT_LAYER_OCR_REQUIRED")
    fields["document_type"] = "INVOICE"
    return fields, confidence, notes


def extract_document(data: bytes, filename: str, content_type: str) -> tuple[dict, dict[str, float], list[str], str, str]:
    """Extract candidate invoice fields from a PDF, email message, or plain text file."""
    normalized_type = content_type.split(";", 1)[0].strip().casefold()
    email_vendor = None
    extraction_notes: list[str] = []
    if normalized_type == "message/rfc822" or filename.casefold().endswith(".eml"):
        message = message_from_bytes(data, policy=default)
        email_vendor = message.get("From")
        text_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_content()
                    if isinstance(payload, str):
                        text_parts.append(payload)
                elif part.get_content_type() == "application/pdf":
                    pdf_text, ocr_used, ocr_note = _pdf_text(part.get_payload(decode=True) or b"")
                    text_parts.append(pdf_text)
                    if ocr_used:
                        extraction_notes.append(ocr_note)
                    elif ocr_note:
                        extraction_notes.append(ocr_note)
        elif message.get_content_type() == "text/plain":
            payload = message.get_content()
            text_parts.append(payload if isinstance(payload, str) else "")
        if message.get("Subject"):
            text_parts.insert(0, f"Subject: {message['Subject']}")
        text = "\n".join(text_parts)
        normalized_type = "message/rfc822"
    elif normalized_type == "application/pdf" or filename.casefold().endswith(".pdf"):
        text, ocr_used, ocr_note = _pdf_text(data)
        if ocr_used or ocr_note:
            extraction_notes.append(ocr_note)
        normalized_type = "application/pdf"
    elif normalized_type in {"text/plain", "application/octet-stream"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Text document must be UTF-8 encoded") from exc
        normalized_type = "text/plain"
    else:
        raise ValueError("Supported document types are PDF, RFC 822 email (.eml), and UTF-8 text")

    fields, confidence, notes = _extract_fields(text, email_vendor)
    notes.extend(extraction_notes)
    digest = hashlib.sha256(data).hexdigest()
    safe_filename = filename.replace("/", "_").replace("\\", "_")[:255] or "invoice-document"
    return fields, confidence, notes, digest, normalized_type
