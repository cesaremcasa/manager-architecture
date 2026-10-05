from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.domain.sales import Sale, aggregate_sales


class InsightSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class DailyMetrics:
    business_date: date
    gross_sales: Decimal
    net_sales: Decimal
    sale_count: int
    tax: Decimal
    tips: Decimal
    service_charges: Decimal
    prior_same_weekday_sales: Decimal

    @property
    def change_vs_prior(self) -> Decimal | None:
        if self.prior_same_weekday_sales == 0:
            return None
        return (self.net_sales - self.prior_same_weekday_sales) / self.prior_same_weekday_sales


@dataclass(frozen=True)
class Insight:
    business_date: date
    kind: str
    severity: InsightSeverity
    source: str
    title: str
    payload: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {"business_date": self.business_date.isoformat(), **self.payload}


def compute_daily_metrics(
    sales: list[Sale], target: date, timezone: str, comparable_sales: list[Decimal] | None = None
) -> DailyMetrics:
    facts = aggregate_sales(sales, target, timezone)
    historical = [value for value in (comparable_sales or []) if value != Decimal("0")]
    prior = sum(historical, Decimal("0")) / len(historical) if historical else Decimal("0")
    return DailyMetrics(
        business_date=target,
        gross_sales=facts["gross_sales"],
        net_sales=facts["net_sales"],
        sale_count=int(facts["sale_count"]),
        tax=facts["tax"],
        tips=facts["tips"],
        service_charges=facts["service_charges"],
        prior_same_weekday_sales=prior,
    )


def build_sales_insights(metrics: DailyMetrics) -> tuple[Insight, ...]:
    change = metrics.change_vs_prior
    if change is None:
        return ()
    change_pct = change * Decimal("100")
    if change <= Decimal("-0.15"):
        return (
            Insight(
                business_date=metrics.business_date,
                kind="sales_variance",
                severity=InsightSeverity.WARNING,
                source="deterministic_metrics",
                title="Net sales are below the comparable-day average",
                payload={"change_pct": str(change_pct.quantize(Decimal("0.1"))), "net_sales": str(metrics.net_sales)},
            ),
        )
    if change >= Decimal("0.15"):
        return (
            Insight(
                business_date=metrics.business_date,
                kind="sales_variance",
                severity=InsightSeverity.INFO,
                source="deterministic_metrics",
                title="Net sales are above the comparable-day average",
                payload={"change_pct": str(change_pct.quantize(Decimal("0.1"))), "net_sales": str(metrics.net_sales)},
            ),
        )
    return ()
