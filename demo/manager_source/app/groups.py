"""Authorization primitives for group-level, read-consolidated Manager views."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class GroupUnit:
    tenant_id: UUID
    unit_code: str
    display_name: str
    seats: int
    city: str
    state: str
    latitude: float | None
    longitude: float | None
    member_role: str


class GroupAccessError(PermissionError):
    """Raised when a signed-in owner has no membership in the requested group."""


class GroupRoleError(GroupAccessError):
    """Raised when a group member lacks the role required by an operation."""


def list_groups_for_owner(engine: Engine, owner_id: UUID) -> list[dict[str, object]]:
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT group_id, group_name, operating_concept, timezone, currency, role FROM list_owner_groups(:owner_id)"),
            {"owner_id": str(owner_id)},
        ).mappings().all()
    return [
        {
            "id": str(row["group_id"]),
            "name": str(row["group_name"]),
            "operating_concept": str(row["operating_concept"]),
            "timezone": str(row["timezone"]),
            "currency": str(row["currency"]),
            "role": str(row["role"]),
        }
        for row in rows
    ]


def require_group_units(engine: Engine, owner_id: UUID, group_id: UUID) -> tuple[GroupUnit, ...]:
    """Resolve membership via a security-definer function before group queries."""
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT tenant_id, unit_code, display_name, seats, city, state, latitude, longitude, member_role
                FROM list_authorized_group_units(:owner_id, :group_id)
                """
            ),
            {"owner_id": str(owner_id), "group_id": str(group_id)},
        ).mappings().all()
    if not rows:
        raise GroupAccessError("group membership is required")
    return tuple(
        GroupUnit(
            tenant_id=UUID(str(row["tenant_id"])),
            unit_code=str(row["unit_code"]),
            display_name=str(row["display_name"]),
            seats=int(row["seats"]),
            city=str(row["city"]),
            state=str(row["state"]),
            latitude=float(row["latitude"]) if row["latitude"] is not None else None,
            longitude=float(row["longitude"]) if row["longitude"] is not None else None,
            member_role=str(row["member_role"]),
        )
        for row in rows
    )


def require_group_role(
    engine: Engine, owner_id: UUID, group_id: UUID, allowed_roles: frozenset[str]
) -> tuple[GroupUnit, ...]:
    """Resolve membership and enforce one consistent role for the group action."""
    units = require_group_units(engine, owner_id, group_id)
    roles = {unit.member_role for unit in units}
    if len(roles) != 1 or not roles.issubset(allowed_roles):
        raise GroupRoleError("the group role does not permit this operation")
    return units
