"""Run the original Manager calculation and MCP read path on local fixtures."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.db import create_app_engine
from app.domain.sales import Sale, aggregate_sales
from app.groups import require_group_role
from app.mcp_gateway import handle_jsonrpc


DATABASE_URL = os.getenv(
    "MANAGER_DEMO_DATABASE_URL",
    "postgresql+psycopg://app_user:demo_password@localhost:55432/manager_demo",
)
GROUP_ID = UUID("40000000-0000-4000-8000-000000000001")
OWNER_ID = UUID("20000000-0000-4000-8000-000000000001")
TARGET_DATE = datetime.now(timezone.utc).date()


def main() -> None:
    engine = create_app_engine(DATABASE_URL)
    with engine.connect() as connection:
        database, role = connection.execute(text("SELECT current_database(), current_user")).one()
        if database != "manager_demo" or role != "app_user":
            raise RuntimeError("demo must connect as app_user to the local manager_demo database")

    require_group_role(engine, OWNER_ID, GROUP_ID, frozenset({"group_owner"}))
    response = handle_jsonrpc(
        engine,
        GROUP_ID,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_group_overview", "arguments": {}}},
    )
    synthetic_sales = aggregate_sales(
        [Sale(datetime.now(timezone.utc), Decimal("900.00"), discounts=Decimal("87.60"))],
        TARGET_DATE,
        "UTC",
    )
    print(json.dumps({"decimal_calculation": synthetic_sales, "mcp_read": response["result"]["structuredContent"]}, default=str, indent=2))
    engine.dispose()


if __name__ == "__main__":
    main()
