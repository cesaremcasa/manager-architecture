import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction


class DailySummaryParseError(ValueError):
    pass


@dataclass(frozen=True)
class DailySalesSummary:
    source: str
    business_date: date
    gross_sales: Decimal
    discounts: Decimal = Decimal("0")
    comps: Decimal = Decimal("0")
    refunds: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    tips: Decimal = Decimal("0")
    service_charges: Decimal = Decimal("0")

    @property
    def source_sale_id(self) -> str:
        return f"{self.source}:{self.business_date.isoformat()}"


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).strip()


def _money(value: str) -> Decimal:
    cleaned = value.strip().replace("$", "").replace(",", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as error:
        raise DailySummaryParseError(f"invalid money value: {value}") from error
    return -amount if negative else amount


def _date(value: str) -> date:
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise DailySummaryParseError(f"invalid business date: {value}")


def parse_daily_summary(body: str, source: str) -> DailySalesSummary:
    source_key = _key(source)
    if source_key not in {"toast", "square"}:
        raise DailySummaryParseError("source must be toast or square")
    values: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^\s*([^:\t]+)\s*[:\t]\s*(.+?)\s*$", line)
        if match:
            values[_key(match.group(1))] = match.group(2)
    date_value = values.get("business date") or values.get("date")
    if date_value is None:
        raise DailySummaryParseError("summary has no business date")
    gross_value = values.get("gross sales") or values.get("total sales") or values.get("sales")
    if gross_value is None:
        raise DailySummaryParseError("summary has no gross or total sales")

    def optional(*keys: str) -> Decimal:
        value = next((values[key] for key in keys if key in values), None)
        return _money(value) if value is not None else Decimal("0")

    return DailySalesSummary(
        source=source_key,
        business_date=_date(date_value),
        gross_sales=_money(gross_value),
        discounts=optional("discounts", "discounts and comps"),
        comps=optional("comps", "complimentary sales"),
        refunds=optional("refunds", "returns").copy_abs(),
        tax=optional("tax", "taxes"),
        tips=optional("tips", "gratuity"),
        service_charges=optional("service charges", "service charge"),
    )


def detect_summary_source(subject: str, body: str) -> str | None:
    envelope = _key(f"{subject} {body[:2000]}")
    for candidate in ("toast", "square"):
        if candidate in envelope and ("daily summary" in envelope or "performance summary" in envelope):
            return candidate
    return None


def ingest_summary_from_inbox_item(engine: Engine, tenant_id: UUID, inbox_item_id: UUID) -> bool:
    with tenant_transaction(engine, tenant_id) as connection:
        row = connection.execute(
            text("SELECT subject, body_text FROM inbox_items WHERE id = :item_id"),
            {"item_id": str(inbox_item_id)},
        ).mappings().one()
    subject = str(row["subject"])
    body = str(row["body_text"])
    source = detect_summary_source(subject, body)
    if source is None:
        return False
    persist_daily_summary(engine, tenant_id, parse_daily_summary(body, source))
    return True


def persist_daily_summary(engine: Engine, tenant_id: UUID, summary: DailySalesSummary) -> None:
    with tenant_transaction(engine, tenant_id) as connection:
        timezone_name = connection.execute(
            text("SELECT timezone FROM restaurants WHERE id = :tenant_id"),
            {"tenant_id": str(tenant_id)},
        ).scalar_one()
        occurred_at = datetime.combine(summary.business_date, time(12), ZoneInfo(str(timezone_name)))
        connection.execute(
            text("""
                INSERT INTO sales
                  (tenant_id, source, source_sale_id, occurred_at, gross_sales, discounts, comps, refunds,
                   tax, tips, service_charges, granularity)
                VALUES (:tenant_id, 'pos_email_summary', :source_sale_id, :occurred_at, :gross_sales,
                        :discounts, :comps, :refunds, :tax, :tips, :service_charges, 'low_granularity')
                ON CONFLICT (tenant_id, source, source_sale_id) DO UPDATE SET
                  occurred_at = EXCLUDED.occurred_at, gross_sales = EXCLUDED.gross_sales,
                  discounts = EXCLUDED.discounts, comps = EXCLUDED.comps, refunds = EXCLUDED.refunds,
                  tax = EXCLUDED.tax, tips = EXCLUDED.tips, service_charges = EXCLUDED.service_charges,
                  granularity = EXCLUDED.granularity
            """),
            {
                "tenant_id": str(tenant_id),
                "source_sale_id": summary.source_sale_id,
                "occurred_at": occurred_at,
                "gross_sales": summary.gross_sales,
                "discounts": summary.discounts,
                "comps": summary.comps,
                "refunds": summary.refunds,
                "tax": summary.tax,
                "tips": summary.tips,
                "service_charges": summary.service_charges,
            },
        )
