from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
from typing import Protocol


@dataclass(frozen=True)
class BriefFacts:
    business_date: date
    net_sales: Decimal
    sale_count: int
    prior_same_weekday_sales: Decimal

    @property
    def change_vs_prior(self) -> Decimal | None:
        if self.prior_same_weekday_sales == 0:
            return None
        return (self.net_sales - self.prior_same_weekday_sales) / self.prior_same_weekday_sales


class BriefGenerator(Protocol):
    def generate(self, facts: BriefFacts) -> str:
        """Return prose that must preserve the supplied numeric facts."""


@dataclass
class BriefCircuitBreaker:
    failure_limit: int = 3
    failures: int = 0
    open: bool = False

    def allow(self) -> bool:
        return not self.open

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_limit:
            self.open = True


def validate_brief_numbers(rendered: str, facts: BriefFacts) -> bool:
    """Ensure generated prose contains the exact auditable values from the facts object."""
    required = (
        facts.business_date.isoformat(),
        f"${facts.net_sales:,.2f}",
        f"Pedidos: {facts.sale_count}",
    )
    return all(re.search(re.escape(value), rendered) is not None for value in required)


def render_template(facts: BriefFacts) -> str:
    change = facts.change_vs_prior
    comparison = "não há histórico comparável"
    if change is not None:
        comparison = f"{change * 100:+.1f}% contra a média do mesmo dia da semana"
    return (
        f"Daily Manager Brief — {facts.business_date.isoformat()}\n\n"
        f"Vendas líquidas: ${facts.net_sales:,.2f}\n"
        f"Pedidos: {facts.sale_count}\n"
        f"Comparativo: {comparison}.\n\n"
        "Próxima ação: revisar as vendas e confirmar as tarefas críticas do dia."
    )


def render_validated_brief(
    facts: BriefFacts,
    generator: BriefGenerator | None = None,
    breaker: BriefCircuitBreaker | None = None,
) -> str:
    """Use generated prose only when it preserves facts; otherwise return the safe template."""
    fallback = render_template(facts)
    if generator is None or (breaker is not None and not breaker.allow()):
        return fallback
    try:
        candidate = generator.generate(facts)
    except Exception:
        if breaker is not None:
            breaker.record_failure()
        return fallback
    if not validate_brief_numbers(candidate, facts):
        if breaker is not None:
            breaker.record_failure()
        return fallback
    if breaker is not None:
        breaker.record_success()
    return candidate
