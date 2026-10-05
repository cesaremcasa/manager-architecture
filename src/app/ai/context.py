"""Build a small, safe, tenant-scoped context for the Manager agent."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction


def build_agent_context(engine: Engine, tenant_id: UUID) -> dict[str, object]:
    with tenant_transaction(engine, tenant_id) as connection:
        inbox = connection.execute(
            text(
                "SELECT count(*) AS total, count(*) FILTER "
                "(WHERE category = 'unknown') AS needs_review FROM inbox_items"
            )
        ).mappings().one()
        sales = connection.execute(
            text(
                "SELECT coalesce(sum(gross_sales - discounts - comps - voids - refunds), 0) "
                "AS net_sales, count(*) AS orders FROM sales WHERE voids = 0"
            )
        ).mappings().one()
        delivery = connection.execute(
            text(
                "SELECT coalesce(sum(net_payout), 0) AS net_payout, "
                "count(*) AS orders FROM delivery_orders"
            )
        ).mappings().one()
        market = connection.execute(
            text("SELECT count(DISTINCT provider_business_id) AS businesses FROM market_observations")
        ).mappings().one()
        tasks = connection.execute(
            text("SELECT count(*) FILTER (WHERE status != 'completed') AS open, count(*) FILTER (WHERE food_safety AND status != 'completed') AS food_safety_open FROM task_instances")
        ).mappings().one()
        production = connection.execute(
            text("SELECT count(*) FILTER (WHERE status != 'completed') AS open_batches FROM production_batches")
        ).mappings().one()
        workforce = connection.execute(
            text(
                "SELECT count(*) FILTER (WHERE employee_id IS NULL AND status != 'cancelled') AS open_shifts, "
                "count(DISTINCT employee_id) FILTER (WHERE status != 'cancelled') AS scheduled_people "
                "FROM workforce_shifts"
            )
        ).mappings().one()
        inventory = connection.execute(
            text(
                "SELECT count(*) AS tracked, "
                "count(*) FILTER (WHERE current_quantity <= reorder_point) AS below_reorder "
                "FROM inventory_items"
            )
        ).mappings().one()
        invoices = connection.execute(
            text(
                "SELECT count(*) FILTER (WHERE status = 'pending') AS pending, "
                "coalesce(sum(total) FILTER (WHERE status = 'pending'), 0) AS pending_total "
                "FROM invoices"
            )
        ).mappings().one()
        recipes = connection.execute(text("SELECT count(*) AS count FROM recipes WHERE active")).mappings().one()
        latest_sales = connection.execute(
            text(
                "SELECT business_date, coalesce(sum(gross_sales - discounts - comps - voids - refunds), 0) AS net_sales, "
                "coalesce(sum(gross_sales), 0) AS gross_sales, coalesce(sum(refunds), 0) AS refunds, "
                "count(*) FILTER (WHERE voids = 0) AS orders "
                "FROM sales WHERE business_date = (SELECT max(business_date) FROM sales) "
                "GROUP BY business_date"
            )
        ).mappings().one_or_none()
    return {
        "inbox": {
            "open_items": int(inbox["total"]),
            "needs_review": int(inbox["needs_review"]),
        },
        "sales": {
            "net_sales": _decimal_text(sales["net_sales"]),
            "orders": int(sales["orders"]),
            "latest_business_date": None if latest_sales is None else str(latest_sales["business_date"]),
            "latest_net_sales": "0" if latest_sales is None else _decimal_text(latest_sales["net_sales"]),
            "latest_gross_sales": "0" if latest_sales is None else _decimal_text(latest_sales["gross_sales"]),
            "latest_refunds": "0" if latest_sales is None else _decimal_text(latest_sales["refunds"]),
            "latest_orders": 0 if latest_sales is None else int(latest_sales["orders"]),
        },
        "delivery": {
            "net_payout": _decimal_text(delivery["net_payout"]),
            "orders": int(delivery["orders"]),
        },
        "market": {"businesses": int(market["businesses"])},
        "tasks": {"open": int(tasks["open"]), "food_safety_open": int(tasks["food_safety_open"])},
        "production": {"open_batches": int(production["open_batches"])},
        "workforce": {
            "open_shifts": int(workforce["open_shifts"]),
            "scheduled_people": int(workforce["scheduled_people"]),
        },
        "inventory": {"tracked": int(inventory["tracked"]), "below_reorder": int(inventory["below_reorder"])},
        "invoices": {"pending": int(invoices["pending"]), "pending_total": _decimal_text(invoices["pending_total"])},
        "recipes": {"count": int(recipes["count"])},
    }


def _decimal_text(value: object) -> str:
    return str(value if isinstance(value, Decimal) else Decimal(str(value)))
