"""Provider-independent invoice extraction contracts and deterministic checks.

OCR output is untrusted input.  This module deliberately contains no provider,
network, database, or secret handling: it turns extracted values into a small
domain contract, validates money with ``Decimal``, and chooses the next review
tier when the extraction is not safe to accept automatically.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Final


CENT: Final = Decimal("0.01")
FLASH_MIN_CONFIDENCE: Final = Decimal("0.90")
PRO_MIN_CONFIDENCE: Final = Decimal("0.95")


def _money(value: Decimal | str | int) -> Decimal:
    """Convert a monetary value to cents without ever going through float."""

    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("money values must be finite Decimal-compatible numbers") from error
    if not amount.is_finite():
        raise ValueError("money values must be finite")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _non_negative(value: Decimal, name: str) -> Decimal:
    amount = _money(value)
    if amount < 0:
        raise ValueError(f"{name} must be non-negative")
    return amount


def _confidence(value: Decimal | str | int) -> Decimal:
    try:
        confidence = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("confidence must be a finite Decimal-compatible number") from error
    if not confidence.is_finite() or not Decimal("0") <= confidence <= Decimal("1"):
        raise ValueError("confidence must be between 0 and 1")
    return confidence


@dataclass(frozen=True)
class InvoiceLine:
    """A line item extracted from an invoice.

    ``line_total`` is the amount printed on the invoice.  For a catch-weight
    item, ``actual_weight`` is the billed quantity and is required because the
    ordered quantity is not sufficient to reproduce the amount.
    """

    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    discount: Decimal = Decimal("0")
    catch_weight: bool = False
    actual_weight: Decimal | None = None

    def __post_init__(self) -> None:
        quantity = _non_negative(self.quantity, "quantity")
        unit_price = _non_negative(self.unit_price, "unit_price")
        line_total = _non_negative(self.line_total, "line_total")
        discount = _non_negative(self.discount, "discount")
        if not self.description.strip():
            raise ValueError("description must not be empty")
        if self.catch_weight:
            actual_weight = (
                None if self.actual_weight is None else _non_negative(self.actual_weight, "actual_weight")
            )
        elif self.actual_weight is not None:
            raise ValueError("actual_weight is only valid for catch-weight lines")
        else:
            actual_weight = None
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "unit_price", unit_price)
        object.__setattr__(self, "line_total", line_total)
        object.__setattr__(self, "discount", discount)
        object.__setattr__(self, "actual_weight", actual_weight)

    @property
    def billed_quantity(self) -> Decimal | None:
        return self.actual_weight if self.catch_weight else self.quantity

    @property
    def calculated_total(self) -> Decimal | None:
        if self.billed_quantity is None:
            return None
        return _money(self.billed_quantity * self.unit_price - self.discount)


@dataclass(frozen=True)
class Invoice:
    """Provider-neutral invoice extraction result."""

    vendor_name: str
    invoice_number: str
    invoice_date: date
    lines: tuple[InvoiceLine, ...]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    discounts: Decimal = Decimal("0")
    other_charges: Decimal = Decimal("0")
    currency: str = "USD"
    confidence: Decimal = Decimal("1.00")

    def __post_init__(self) -> None:
        for name in ("subtotal", "tax", "total", "discounts", "other_charges"):
            object.__setattr__(self, name, _non_negative(getattr(self, name), name))
        confidence = _confidence(self.confidence)
        if not self.vendor_name.strip() or not self.invoice_number.strip():
            raise ValueError("vendor_name and invoice_number must not be empty")
        if not self.lines:
            raise ValueError("invoice must contain at least one line")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True)
class InvoiceArithmetic:
    valid: bool
    errors: tuple[str, ...] = ()
    calculated_subtotal: Decimal = Decimal("0")
    calculated_total: Decimal = Decimal("0")


def validate_invoice_arithmetic(invoice: Invoice) -> InvoiceArithmetic:
    """Validate line extensions, subtotal, and the invoice grand total.

    All comparisons are exact at cent precision.  The declared ``subtotal`` is
    the pre-invoice-discount sum of line totals; the expected grand total is
    ``subtotal - discounts + tax + other_charges``.
    """

    errors: list[str] = []
    line_subtotal = Decimal("0")
    for index, line in enumerate(invoice.lines, start=1):
        if line.catch_weight and line.actual_weight is None:
            errors.append(f"line {index} catch-weight actual_weight is missing")
            continue
        calculated = line.calculated_total
        assert calculated is not None
        if calculated != line.line_total:
            errors.append(f"line {index} total mismatch: expected {calculated}, got {line.line_total}")
        line_subtotal += line.line_total
    calculated_subtotal = _money(line_subtotal)
    if calculated_subtotal != invoice.subtotal:
        errors.append(f"subtotal mismatch: expected {calculated_subtotal}, got {invoice.subtotal}")
    calculated_total = _money(
        invoice.subtotal - invoice.discounts + invoice.tax + invoice.other_charges
    )
    if calculated_total != invoice.total:
        errors.append(f"total mismatch: expected {calculated_total}, got {invoice.total}")
    if invoice.discounts > invoice.subtotal:
        errors.append("discounts cannot exceed subtotal")
    return InvoiceArithmetic(
        valid=not errors,
        errors=tuple(errors),
        calculated_subtotal=calculated_subtotal,
        calculated_total=calculated_total,
    )


class InvoiceReviewTier(StrEnum):
    FLASH = "flash"
    PRO = "pro"
    HUMAN = "human"


@dataclass(frozen=True)
class ExtractionAssessment:
    arithmetic_valid: bool
    confidence: Decimal
    required_fields_complete: bool = True
    security_flag: bool = False

    def __post_init__(self) -> None:
        confidence = _confidence(self.confidence)
        object.__setattr__(self, "confidence", confidence)

    def passes(self, minimum_confidence: Decimal) -> bool:
        return (
            self.arithmetic_valid
            and self.required_fields_complete
            and not self.security_flag
            and self.confidence >= minimum_confidence
        )


def decide_invoice_review(
    flash: ExtractionAssessment,
    pro: ExtractionAssessment | None = None,
) -> InvoiceReviewTier:
    """Choose the lowest-cost safe review tier.

    Flash is accepted only when complete, arithmetically valid, and at least
    0.90 confident.  Any failure or low confidence escalates to Pro.  Pro must
    meet the stricter 0.95 threshold; otherwise a human must review it.
    """

    if flash.passes(FLASH_MIN_CONFIDENCE):
        return InvoiceReviewTier.FLASH
    if pro is None:
        return InvoiceReviewTier.PRO
    if pro.passes(PRO_MIN_CONFIDENCE):
        return InvoiceReviewTier.PRO
    return InvoiceReviewTier.HUMAN
