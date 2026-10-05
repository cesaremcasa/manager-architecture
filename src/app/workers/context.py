from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.engine import Connection, Engine

from app.db import tenant_transaction


@contextmanager
def worker_tenant_transaction(engine: Engine, tenant_id: UUID) -> Iterator[Connection]:
    """Worker entrypoint: every job must carry an explicit tenant_id."""
    with tenant_transaction(engine, tenant_id) as connection:
        yield connection

