"""Administrator endpoints: outbox inspection and retry, and integration status."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import AdminUserDep, SessionDep
from app.core.config import Settings
from app.core.domain_errors import ConcurrencyConflictError, NotFoundError
from app.core.time import utcnow
from app.models.enums import OutboxStatus
from app.repositories.outbox_repository import OutboxRepository
from app.schemas.telegram import IntegrationStatusAdmin, OutboxEventAdmin, OutboxListAdmin
from app.services.outbox_service import OutboxService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/outbox", response_model=OutboxListAdmin, summary="Recent outbox events")
async def list_outbox(
    admin: AdminUserDep,
    session: SessionDep,
    status: OutboxStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OutboxListAdmin:
    rows = await OutboxRepository(session).list_recent(status, limit)
    return OutboxListAdmin(
        data=[
            OutboxEventAdmin(
                id=row.id,
                event_type=row.event_type,
                aggregate_id=row.aggregate_id,
                status=row.status.value,
                attempt_count=row.attempt_count,
                last_error_code=row.last_error_code,
                next_attempt_at=row.next_attempt_at,
                created_at=row.created_at,
                processed_at=row.processed_at,
            )
            for row in rows
        ]
    )


@router.post("/outbox/{event_id}/retry", summary="Re-queue a failed outbox event")
async def retry_outbox_event(event_id: int, admin: AdminUserDep, session: SessionDep) -> dict[str, str]:
    service = OutboxService(session)
    if await service.get(event_id) is None:
        raise NotFoundError("Outbox event not found.")
    if not await service.admin_retry(event_id, now=utcnow()):
        raise ConcurrencyConflictError("Only failed events can be retried.")
    return {"status": "queued"}


@router.get("/payments/reconciliation", summary="Open payments compared with the provider")
async def payment_reconciliation(
    request: Request, admin: AdminUserDep, session: SessionDep
) -> dict[str, object]:
    from app.services.payment_service import PaymentService

    items = await PaymentService(session, request.app.state.payment_provider).reconcile()
    return {
        "data": [asdict(item) for item in items],
        "mismatches": sum(1 for item in items if item.mismatch),
    }


@router.get("/integrations/telegram", response_model=IntegrationStatusAdmin)
async def telegram_status(
    request: Request, admin: AdminUserDep, session: SessionDep
) -> IntegrationStatusAdmin:
    settings: Settings = request.app.state.settings
    return IntegrationStatusAdmin(
        telegram_configured=settings.mtproto_configured,
        worker_enabled=settings.worker_active,
        outbox_counts=await OutboxService(session).count_by_status(),
    )
