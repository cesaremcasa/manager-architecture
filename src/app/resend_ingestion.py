import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction
from app.inbound_security import InboundLimits, redact_error, validate_magic_bytes
from app.inbox import parse_inbox_address
from app.resend_client import ResendReceivingClient


def event_email_id(event: Mapping[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("email_id"), str):
        raise ValueError("email.received event has no email_id")
    return data["email_id"]


def event_recipient(event: Mapping[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("webhook event has no data")
    for key in ("received_for", "to"):
        values = data.get(key, [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, Sequence):
            for value in values:
                if isinstance(value, str):
                    try:
                        parse_inbox_address(value)
                    except ValueError:
                        continue
                    return value
    raise LookupError("event has no Manager Inbox recipient")


def resolve_tenant_for_event(engine: Engine, event: Mapping[str, Any]) -> UUID:
    from app.ingestion import resolve_tenant_for_recipient

    return resolve_tenant_for_recipient(engine, event_recipient(event))


def persist_webhook_event(
    engine: Engine,
    tenant_id: UUID,
    *,
    svix_id: str,
    event: Mapping[str, Any],
) -> bool:
    with tenant_transaction(engine, tenant_id) as connection:
        row = connection.execute(
            text(
                """
                INSERT INTO resend_webhook_events
                  (tenant_id, svix_id, event_type, email_id, payload)
                VALUES (:tenant_id, :svix_id, :event_type, :email_id, CAST(:payload AS jsonb))
                ON CONFLICT (tenant_id, svix_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "svix_id": svix_id,
                "event_type": str(event.get("type", "unknown")),
                "email_id": event_email_id(event),
                "payload": json.dumps(event),
            },
        ).scalar_one_or_none()
        return row is not None


async def process_received_email(
    engine: Engine,
    tenant_id: UUID,
    *,
    svix_id: str,
    email_id: str,
    api_key: str,
    max_download_bytes: int | None = None,
) -> UUID:
    limits = InboundLimits.from_env()
    if max_download_bytes is not None:
        limits = InboundLimits(
            raw_mime_bytes=max_download_bytes,
            attachment_bytes=max_download_bytes,
            total_attachment_bytes=max_download_bytes,
            attachment_count=limits.attachment_count,
        )
    client = ResendReceivingClient(api_key)
    try:
        email = await client.get_received_email(email_id)
        sender = str(email.get("from") or "unknown@resend")
        message_id = str(email.get("message_id") or email_id)
        headers = email.get("headers") if isinstance(email.get("headers"), Mapping) else {}
        raw_mime = await _download_raw(client, email.get("raw"), limits.raw_mime_bytes)
        attachments = await _download_attachments(
            client, email_id, email.get("attachments"), limits
        )

        with tenant_transaction(engine, tenant_id) as connection:
            item_id = connection.execute(
                text(
                    """
                    INSERT INTO inbox_items
                      (tenant_id, message_id, provider_email_id, sender, subject, body_text,
                       body_html, headers)
                    VALUES (:tenant_id, :message_id, :provider_email_id, :sender, :subject,
                            :body_text, :body_html, CAST(:headers AS jsonb))
                    ON CONFLICT (tenant_id, message_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": str(tenant_id),
                    "message_id": message_id,
                    "provider_email_id": email_id,
                    "sender": sender,
                    "subject": str(email.get("subject") or ""),
                    "body_text": str(email.get("text") or ""),
                    "body_html": str(email.get("html") or ""),
                    "headers": json.dumps(headers),
                },
            ).scalar_one_or_none()
            if item_id is None:
                item_id = connection.execute(
                    text("SELECT id FROM inbox_items WHERE message_id = :message_id"),
                    {"message_id": message_id},
                ).scalar_one()

            if raw_mime is not None:
                connection.execute(
                    text(
                        """
                        UPDATE inbox_items
                        SET raw_mime = COALESCE(raw_mime, :raw_mime),
                            raw_mime_sha256 = COALESCE(raw_mime_sha256, :sha256),
                            raw_mime_size = COALESCE(raw_mime_size, :size)
                        WHERE id = :item_id
                        """
                    ),
                    {
                        "item_id": str(item_id),
                        "raw_mime": raw_mime,
                        "sha256": hashlib.sha256(raw_mime).hexdigest(),
                        "size": len(raw_mime),
                    },
                )
            for attachment in attachments:
                connection.execute(
                    text(
                        """
                        INSERT INTO inbox_attachments
                          (tenant_id, inbox_item_id, provider_attachment_id, filename,
                           content_type, content_disposition, content_id, size, content, content_sha256)
                        VALUES (:tenant_id, :item_id, :provider_id, :filename, :content_type,
                                :content_disposition, :content_id, :size, :content, :sha256)
                        ON CONFLICT (tenant_id, inbox_item_id, provider_attachment_id)
                        DO UPDATE SET content = COALESCE(inbox_attachments.content, EXCLUDED.content),
                                      content_sha256 = COALESCE(inbox_attachments.content_sha256, EXCLUDED.content_sha256)
                        """
                    ),
                    {"tenant_id": str(tenant_id), "item_id": str(item_id), **attachment},
                )
            connection.execute(
                text(
                    """
                    UPDATE resend_webhook_events
                    SET status = 'processed', processed_at = now(), claimed_at = NULL,
                        enqueued_at = NULL, error_message = NULL
                    WHERE tenant_id = :tenant_id AND svix_id = :svix_id
                    """
                ),
                {"tenant_id": str(tenant_id), "svix_id": svix_id},
            )
            return UUID(str(item_id))
    except Exception as error:
        with tenant_transaction(engine, tenant_id) as connection:
            connection.execute(
                text(
                    """
                    UPDATE resend_webhook_events
                    SET status = 'failed', claimed_at = NULL,
                        next_attempt_at = now() + interval '5 minutes',
                        error_message = :error
                    WHERE tenant_id = :tenant_id AND svix_id = :svix_id
                    """
                ),
                {
                    "tenant_id": str(tenant_id),
                    "svix_id": svix_id,
                    "error": redact_error(str(error)),
                },
            )
        raise


async def _download_raw(
    client: ResendReceivingClient, raw: Any, limit: int
) -> bytes | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("download_url"), str):
        return None
    content = await client.download(raw["download_url"])
    if len(content) > limit:
        raise ValueError("raw MIME exceeds configured size limit")
    return content


async def _download_attachments(
    client: ResendReceivingClient,
    email_id: str,
    metadata: Any,
    limits: InboundLimits,
) -> list[dict[str, Any]]:
    if not isinstance(metadata, list) or not metadata:
        return []
    if len(metadata) > limits.attachment_count:
        raise ValueError("attachment count exceeds configured limit")
    listed = await client.list_attachments(email_id)
    by_id = {str(item.get("id")): item for item in listed if item.get("id")}
    result: list[dict[str, Any]] = []
    total_size = 0
    for item in metadata:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        detail = by_id.get(str(item["id"]), item)
        declared_size = int(item.get("size") or detail.get("size") or 0)
        if declared_size > limits.attachment_bytes:
            raise ValueError("attachment exceeds configured size limit")
        if total_size + declared_size > limits.total_attachment_bytes:
            raise ValueError("total attachment size exceeds configured limit")
        url = detail.get("download_url")
        content = await client.download(url) if isinstance(url, str) else None
        if content is not None and len(content) > limits.attachment_bytes:
            raise ValueError("attachment exceeds configured size limit")
        if content is not None:
            total_size += len(content)
        else:
            total_size += declared_size
        content_type = str(item.get("content_type") or "application/octet-stream")
        if content is not None:
            validate_magic_bytes(content_type, content)
        result.append(
            {
                "provider_id": str(item["id"]),
                "filename": str(item.get("filename") or "attachment"),
                "content_type": str(item.get("content_type") or "application/octet-stream"),
                "content_disposition": item.get("content_disposition"),
                "content_id": item.get("content_id"),
                "size": declared_size or len(content or b""),
                "content": content,
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
        )
    return result
