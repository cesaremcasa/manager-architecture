from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url

from app.db import group_transaction, tenant_transaction
from app.domain.sales import Sale, aggregate_sales
from app.groups import GroupAccessError, GroupRoleError, require_group_role, require_group_units
from app.mcp_gateway import TOOL_DEFINITIONS, handle_jsonrpc
from app.workers.context import worker_tenant_transaction


DATABASE_URL = os.getenv(
    "MANAGER_DEMO_DATABASE_URL",
    "postgresql+psycopg://app_user:demo_password@localhost:55432/manager_demo",
)
GROUP_A = UUID("40000000-0000-4000-8000-000000000001")
GROUP_B = UUID("40000000-0000-4000-8000-000000000002")
OWNER = UUID("20000000-0000-4000-8000-000000000001")
VIEWER = UUID("20000000-0000-4000-8000-000000000002")
OPERATOR = UUID("20000000-0000-4000-8000-000000000003")
TENANT_A = UUID("10000000-0000-4000-8000-000000000001")
TENANT_B = UUID("10000000-0000-4000-8000-000000000002")
RUN_B = UUID("50000000-0000-4000-8000-000000000002")


@pytest.fixture(scope="module")
def engine() -> Engine:
    url = make_url(DATABASE_URL)
    assert url.database == "manager_demo"
    assert url.username == "app_user"
    socket_directory = url.query.get("host", "")
    assert url.host in {"localhost", "127.0.0.1"} or socket_directory.endswith("/manager-pg-socket")
    db_engine = create_engine(DATABASE_URL)
    with db_engine.connect() as connection:
        role = connection.execute(
            text("SELECT current_database(), current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one()
    assert role == ("manager_demo", "app_user", False, False)
    yield db_engine
    db_engine.dispose()


def test_real_postgres_policies_contexts_authorization_and_mcp(engine: Engine) -> None:
    # Fail closed when either local scope is absent.
    with engine.begin() as connection:
        assert connection.execute(text("SELECT unit_code FROM group_units")).all() == []
        assert connection.execute(text("SELECT message_id FROM inbox_items")).all() == []
    with pytest.raises(DBAPIError, match="row-level security"):
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO inbox_items (tenant_id,message_id,sender) VALUES (:tenant,'blocked','x')"),
                {"tenant": str(TENANT_A)},
            )

    with tenant_transaction(engine, TENANT_A) as connection:
        assert connection.execute(text("SELECT message_id FROM inbox_items")).scalars().all() == ["synthetic-cedar"]
        assert connection.execute(
            text("SELECT message_id FROM inbox_items WHERE tenant_id=:tenant"), {"tenant": str(TENANT_B)}
        ).all() == []
        with pytest.raises(DBAPIError, match="row-level security"):
            connection.execute(
                text("INSERT INTO inbox_items (tenant_id,message_id,sender) VALUES (:tenant,'cross-tenant','x')"),
                {"tenant": str(TENANT_B)},
            )

    with worker_tenant_transaction(engine, TENANT_B) as connection:
        assert connection.execute(text("SELECT message_id FROM inbox_items")).scalars().all() == ["synthetic-birch"]
    with pytest.raises(ValueError, match="tenant_id is required"):
        with worker_tenant_transaction(engine, None):  # type: ignore[arg-type]
            pytest.fail("worker context opened without an explicit tenant")

    with group_transaction(engine, GROUP_A) as connection:
        assert connection.execute(text("SELECT unit_code FROM group_units")).scalars().all() == ["CEDAR"]
        assert connection.execute(text("SELECT payload->>'net_sales' FROM integration_records")).scalars().all() == ["812.40"]
        with pytest.raises(DBAPIError, match="row-level security"):
            connection.execute(
                text("INSERT INTO integration_records (group_id,tenant_id,import_run_id,dataset,external_id,source_type,schema_version,payload,payload_hash) VALUES (:group,:tenant,:run,'Sales_Daily_24M','cross-group','synthetic_scenario','demo-v1','{}','blocked')"),
                {"group": str(GROUP_B), "tenant": str(TENANT_B), "run": str(RUN_B)},
            )
    with group_transaction(engine, GROUP_B) as connection:
        assert connection.execute(text("SELECT unit_code FROM group_units")).scalars().all() == ["BIRCH"]

    with pytest.raises(GroupAccessError, match="membership is required"):
        require_group_units(engine, OPERATOR, GROUP_A)
    with pytest.raises(GroupRoleError, match="role does not permit"):
        require_group_role(engine, VIEWER, GROUP_A, frozenset({"group_owner"}))
    assert require_group_role(engine, OWNER, GROUP_A, frozenset({"group_owner"}))
    assert require_group_role(engine, OPERATOR, GROUP_B, frozenset({"group_owner", "operator"}))
    with pytest.raises(GroupRoleError, match="role does not permit"):
        require_group_role(engine, OPERATOR, GROUP_B, frozenset({"group_owner"}))

    # The app-layer membership guard runs before the original read-only gateway.
    require_group_role(engine, OWNER, GROUP_A, frozenset({"group_owner"}))
    response = handle_jsonrpc(
        engine,
        GROUP_A,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_group_overview", "arguments": {}}},
    )
    assert response["result"]["structuredContent"]["units"][0]["unit_code"] == "CEDAR"
    rejected = handle_jsonrpc(
        engine,
        GROUP_A,
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "approve_invoice", "arguments": {}}},
    )
    assert rejected["result"]["isError"] is True
    assert all("approve" not in str(tool["name"]) for tool in TOOL_DEFINITIONS)

    totals = aggregate_sales(
        [Sale(datetime(2026, 10, 5, 12, tzinfo=timezone.utc), Decimal("900.00"), discounts=Decimal("87.60"))],
        date(2026, 10, 5),
        "UTC",
    )
    assert totals["net_sales"] == Decimal("812.40")
