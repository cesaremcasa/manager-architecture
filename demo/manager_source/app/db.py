from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from app.tenant import require_tenant_id


def create_app_engine(database_url: str) -> Engine:
    """Create an engine for app_user; migrations must use a separate admin URL."""
    return create_engine(database_url, pool_pre_ping=True)


@contextmanager
def tenant_transaction(engine: Engine, tenant_id: UUID) -> Iterator[Connection]:
    """Set tenant context inside one transaction and clear it on exit."""
    tenant = require_tenant_id(tenant_id)
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant)})
        yield connection


@contextmanager
def group_transaction(engine: Engine, group_id: UUID) -> Iterator[Connection]:
    """Open a transaction constrained to one restaurant group.

    This is deliberately separate from ``tenant_transaction``.  Group tables
    hold cross-unit read models and integration metadata; operational unit
    tables continue to require the tenant context above.
    """
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.group_id', :group_id, true)"), {"group_id": str(group_id)})
        yield connection
