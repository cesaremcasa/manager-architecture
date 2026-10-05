from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.db import tenant_transaction
from app.inbound_security import redact_error

EnqueueJob = Callable[..., Awaitable[object]]


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: UUID
    tenant_id: UUID
    svix_id: str
    email_id: str


def claim_pending_events(engine: Engine, limit: int = 100) -> list[ClaimedEvent]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT event_id, tenant_id, svix_id, email_id "
                "FROM claim_resend_events(:limit)"
            ),
            {"limit": limit},
        ).mappings().all()
    return [
        ClaimedEvent(
            event_id=UUID(str(row["event_id"])),
            tenant_id=UUID(str(row["tenant_id"])),
            svix_id=str(row["svix_id"]),
            email_id=str(row["email_id"]),
        )
        for row in rows
    ]


async def reconcile_resend_events(
    engine: Engine,
    enqueue_job: EnqueueJob,
    *,
    limit: int = 100,
) -> dict[str, int]:
    claimed = claim_pending_events(engine, limit)
    enqueued = 0
    failed = 0
    for event in claimed:
        try:
            await enqueue_job(
                "receive_resend_email_job",
                str(event.tenant_id),
                event.svix_id,
                event.email_id,
                _job_id=f"resend:{event.svix_id}",
                _queue_name="critical",
            )
        except Exception as error:
            failed += 1
            _mark_enqueue_failed(engine, event, str(error))
        else:
            enqueued += 1
            _mark_enqueued(engine, event)
    return {"claimed": len(claimed), "enqueued": enqueued, "failed": failed}


def _mark_enqueued(engine: Engine, event: ClaimedEvent) -> None:
    with tenant_transaction(engine, event.tenant_id) as connection:
        connection.execute(
            text(
                """
                UPDATE resend_webhook_events
                SET status = 'enqueued', enqueued_at = now(), claimed_at = NULL,
                    error_message = NULL
                WHERE id = :event_id
                """
            ),
            {"event_id": str(event.event_id)},
        )


def _mark_enqueue_failed(engine: Engine, event: ClaimedEvent, error: str) -> None:
    with tenant_transaction(engine, event.tenant_id) as connection:
        connection.execute(
            text(
                """
                UPDATE resend_webhook_events
                SET status = 'failed', claimed_at = NULL,
                    next_attempt_at = now() + (:delay * interval '1 second'),
                    error_message = :error
                WHERE id = :event_id
                """
            ),
            {
                "event_id": str(event.event_id),
                "delay": 60,
                "error": redact_error(error),
            },
        )


def mark_event_processed(engine: Engine, tenant_id: UUID, svix_id: str) -> None:
    with tenant_transaction(engine, tenant_id) as connection:
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


def mark_event_processing_failed(
    engine: Engine,
    tenant_id: UUID,
    svix_id: str,
    error: str,
) -> None:
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
            {"tenant_id": str(tenant_id), "svix_id": svix_id, "error": error[:1000]},
        )
