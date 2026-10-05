"""Untrusted, provider-independent invoice extraction boundary.

Providers only return data.  They never decide whether an invoice is valid or
whether it may be persisted.  This module deliberately accepts mappings rather
than a provider SDK type so a real OCR provider can be added without changing
the domain contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from app.domain.invoices import (
    ExtractionAssessment,
    Invoice,
    InvoiceLine,
    InvoiceReviewTier,
    decide_invoice_review,
    validate_invoice_arithmetic,
)

SCHEMA_VERSION = "invoice-extraction.v1"
_INJECTION_PATTERNS = (
    re.compile(
        r"\b(ignore|disregard|override|forget)\b.{0,80}\b(instruction|rule|prompt|system)\b",
        re.I | re.S,
    ),
    re.compile(r"\b(system|developer)\s*message\s*[:=]", re.I),
    re.compile(r"<\s*(system|instruction|prompt)\s*>|\[\s*(system|instruction)\s*\]", re.I),
)
_MISSING = object()


class InvoiceExtractor(Protocol):
    """The only provider contract needed by the domain."""

    version: str

    def extract(self, document: bytes, *, filename: str | None = None) -> Mapping[str, Any]:
        """Return structured OCR output; the result is always untrusted."""


@dataclass(frozen=True)
class ExtractionMetadata:
    extractor_version: str
    schema_version: str
    input_sha256: str

    @classmethod
    def for_input(
        cls,
        document: bytes,
        *,
        extractor_version: str,
        schema_version: str = SCHEMA_VERSION,
    ) -> "ExtractionMetadata":
        if not extractor_version.strip():
            raise ValueError("extractor_version must not be empty")
        return cls(extractor_version, schema_version, hashlib.sha256(document).hexdigest())

    def as_persistence_fields(self) -> dict[str, str]:
        return {
            "extractor_version": self.extractor_version,
            "schema_version": self.schema_version,
            "input_hash": self.input_sha256,
        }


@dataclass(frozen=True)
class ExtractionResult:
    invoice: Invoice | None
    metadata: ExtractionMetadata
    raw_payload: Mapping[str, Any]
    issues: tuple[str, ...] = ()
    injection_detected: bool = False

    @property
    def arithmetic(self):
        return None if self.invoice is None else validate_invoice_arithmetic(self.invoice)

    @property
    def complete(self) -> bool:
        return self.invoice is not None and not self.issues and not self.injection_detected

    def assessment(self) -> ExtractionAssessment:
        arithmetic_valid = self.arithmetic.valid if self.arithmetic is not None else False
        return ExtractionAssessment(
            arithmetic_valid=arithmetic_valid,
            confidence=self.invoice.confidence if self.invoice is not None else Decimal("0"),
            required_fields_complete=self.complete,
            security_flag=self.injection_detected,
        )

    def persistence_record(self) -> dict[str, Any]:
        """Return append-only fields; callers may persist this unchanged."""
        return {
            **self.metadata.as_persistence_fields(),
            "raw_payload": json.loads(json.dumps(self.raw_payload, default=str)),
            "normalized": None if self.invoice is None else invoice_to_dict(self.invoice),
        }


def extract_invoice(
    extractor: InvoiceExtractor,
    document: bytes,
    *,
    filename: str | None = None,
) -> ExtractionResult:
    """Call a provider once, then treat every returned field as untrusted."""
    payload = extractor.extract(document, filename=filename)
    if not isinstance(payload, Mapping):
        raise TypeError("invoice extractor must return a mapping")
    metadata = ExtractionMetadata.for_input(document, extractor_version=extractor.version)
    return normalize_invoice_payload(payload, metadata=metadata)


def normalize_invoice_payload(
    payload: Mapping[str, Any], *, metadata: ExtractionMetadata
) -> ExtractionResult:
    """Normalize a provider payload using allow-listed fields and no execution."""
    raw_text = json.dumps(payload, ensure_ascii=False, default=str)
    injection = any(pattern.search(raw_text) for pattern in _INJECTION_PATTERNS)
    issues: list[str] = ["prompt injection detected in OCR output"] if injection else []
    try:
        vendor = _text(payload, "vendor_name", "vendor", "supplier", "merchant")
        number = _text(payload, "invoice_number", "invoice_no", "number", "invoice_id")
        invoice_date = _parse_date(_value(payload, "invoice_date", "date"))
        currency = _currency(_value(payload, "currency", "currency_code"))
        lines_raw = _value(payload, "lines", "items", "line_items")
        if not isinstance(lines_raw, Sequence) or isinstance(lines_raw, (str, bytes)):
            raise ValueError("lines must be a list")
        lines = tuple(_line(item, index) for index, item in enumerate(lines_raw, start=1))
        invoice = Invoice(
            vendor_name=vendor,
            invoice_number=number,
            invoice_date=invoice_date,
            lines=lines,
            subtotal=_decimal(payload, "subtotal", "sub_total"),
            discounts=_decimal(payload, "discounts", "discount", default=Decimal("0")),
            tax=_decimal(payload, "tax", "tax_amount", default=Decimal("0")),
            other_charges=_decimal(
                payload, "other_charges", "charges", "shipping", default=Decimal("0")
            ),
            total=_decimal(payload, "total", "grand_total", "amount_due"),
            currency=currency,
            confidence=_decimal(payload, "confidence", default=Decimal("0")),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        issues.append(str(error))
        invoice = None
    return ExtractionResult(invoice, metadata, dict(payload), tuple(issues), injection)


def decide_extraction_tier(
    flash: ExtractionResult, pro: ExtractionResult | None = None
) -> InvoiceReviewTier:
    """Escalate deterministically; a security signal can never be auto-accepted."""
    return decide_invoice_review(flash.assessment(), None if pro is None else pro.assessment())


def invoice_to_dict(invoice: Invoice) -> dict[str, Any]:
    return {
        "vendor_name": invoice.vendor_name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat(),
        "currency": invoice.currency,
        "subtotal": str(invoice.subtotal),
        "discounts": str(invoice.discounts),
        "tax": str(invoice.tax),
        "other_charges": str(invoice.other_charges),
        "total": str(invoice.total),
        "confidence": str(invoice.confidence),
        "lines": [
            {
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "line_total": str(line.line_total),
                "discount": str(line.discount),
                "catch_weight": line.catch_weight,
                "actual_weight": None if line.actual_weight is None else str(line.actual_weight),
            }
            for line in invoice.lines
        ],
    }


def _value(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    if default is not _MISSING:
        return default
    raise KeyError(f"missing required field: {names[0]}")


def _text(mapping: Mapping[str, Any], *names: str) -> str:
    value = _value(mapping, *names)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{names[0]} must be non-empty text")
    return " ".join(value.split())


def _decimal(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Decimal:
    value = _value(mapping, *names, default=default)
    if value is _MISSING:
        raise KeyError(f"missing required field: {names[0]}")
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{names[0]} must be a decimal string or Decimal")
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return Decimal(text)


def _currency(value: Any) -> str:
    symbols = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "R$": "BRL"}
    code = symbols.get(str(value).strip(), str(value).strip().upper())
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency must be a three-letter code")
    return code


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("invoice_date must be ISO or unambiguous US date")


def _line(value: Any, index: int) -> InvoiceLine:
    if not isinstance(value, Mapping):
        raise TypeError(f"line {index} must be an object")
    catch_weight = _boolean(_value(value, "catch_weight", "is_catch_weight", default=False))
    actual = _value(value, "actual_weight", "billed_weight", "weight", default=None)
    return InvoiceLine(
        description=_text(value, "description", "item", "name"),
        quantity=_decimal(value, "quantity", "qty"),
        unit_price=_decimal(value, "unit_price", "price", "unit_cost"),
        line_total=_decimal(value, "line_total", "total", "extended_price"),
        discount=_decimal(value, "discount", default=Decimal("0")),
        catch_weight=catch_weight,
        actual_weight=None if actual is None else _decimal({"value": actual}, "value"),
    )


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0", ""}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError("catch_weight must be boolean")
