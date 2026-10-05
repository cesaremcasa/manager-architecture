from uuid import UUID


def require_tenant_id(tenant_id: UUID | None) -> UUID:
    """Fail closed when a request or job has no explicit tenant context."""
    if tenant_id is None:
        raise ValueError("tenant_id is required")
    return tenant_id

