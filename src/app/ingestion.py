from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction
from app.inbox import parse_inbox_address


@dataclass(frozen=True)
class InboundMessage:
    recipient: str
    message_id: str
    sender: str
    subject: str
    body_text: str = ""


@dataclass(frozen=True)
class IngestionResult:
    inbox_item_id: UUID
    tenant_id: UUID
    created: bool


def resolve_tenant_for_recipient(engine: Engine, recipient: str) -> UUID:
    address = parse_inbox_address(recipient)
    with engine.connect() as connection:
        tenant_id = connection.execute(
            text("SELECT resolve_manager_inbox(:slug, :token)"),
            {"slug": address.slug, "token": address.token},
        ).scalar_one_or_none()
    if tenant_id is None:
        raise LookupError("unknown Manager Inbox address")
    return UUID(str(tenant_id))


def persist_inbound_message(engine: Engine, message: InboundMessage) -> IngestionResult:
    tenant_id = resolve_tenant_for_recipient(engine, message.recipient)
    with tenant_transaction(engine, tenant_id) as connection:
        row = connection.execute(
            text(
                """
                INSERT INTO inbox_items (tenant_id, message_id, sender, subject, body_text)
                VALUES (:tenant_id, :message_id, :sender, :subject, :body_text)
                ON CONFLICT (tenant_id, message_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "message_id": message.message_id,
                "sender": message.sender,
                "subject": message.subject,
                "body_text": message.body_text,
            },
        ).scalar_one_or_none()
        if row is not None:
            return IngestionResult(UUID(str(row)), tenant_id, created=True)
        existing = connection.execute(
            text("SELECT id FROM inbox_items WHERE message_id = :message_id"),
            {"message_id": message.message_id},
        ).scalar_one()
        return IngestionResult(UUID(str(existing)), tenant_id, created=False)

