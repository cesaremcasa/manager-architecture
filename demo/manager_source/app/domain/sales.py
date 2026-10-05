from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Sale:
    occurred_at: datetime
    gross_sales: Decimal
    source_sale_id: str = ""
    discounts: Decimal = Decimal("0")
    comps: Decimal = Decimal("0")
    voids: Decimal = Decimal("0")
    refunds: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    tips: Decimal = Decimal("0")
    service_charges: Decimal = Decimal("0")

    @property
    def money_fields(self) -> tuple[str, ...]:
        return tuple(field.name for field in fields(self) if field.name not in {"occurred_at", "source_sale_id"})

    @property
    def is_voided(self) -> bool:
        return self.voids > 0

    @property
    def net_sales(self) -> Decimal:
        return self.gross_sales - self.discounts - self.comps - self.refunds


def business_date(occurred_at: datetime, timezone: str, cutoff: time = time(4, 0)) -> date:
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    local = occurred_at.astimezone(ZoneInfo(timezone))
    current = local.date()
    if local.time() < cutoff:
        return current - timedelta(days=1)
    return current


def aggregate_sales(sales: list[Sale], target: date, timezone: str) -> dict[str, Decimal | int]:
    rows = [
        sale
        for sale in sales
        if business_date(sale.occurred_at, timezone) == target and not sale.is_voided
    ]
    return {
        "sale_count": len(rows),
        "gross_sales": sum((row.gross_sales for row in rows), Decimal("0")),
        "net_sales": sum((row.net_sales for row in rows), Decimal("0")),
        "tax": sum((row.tax for row in rows), Decimal("0")),
        "tips": sum((row.tips for row in rows), Decimal("0")),
        "service_charges": sum((row.service_charges for row in rows), Decimal("0")),
    }
