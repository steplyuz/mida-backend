"""Deal's Telegram group: status for parties, and an admin-only retry."""

from __future__ import annotations

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUserDep, CurrentUserDep, DealServiceDep, SessionDep
from app.core.domain_errors import ConcurrencyConflictError, InvalidTransitionError
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.enums import MembershipKind, TelegramGroupStatus
from app.models.telegram import TelegramMembership
from app.schemas.telegram import TelegramGroupPublic
from app.services.audit_service import AuditService
from app.services.outbox_service import GROUP_EVENT, OutboxService

router = APIRouter(prefix="/deals", tags=["telegram"])


@router.get("/{public_id}/telegram-group", response_model=TelegramGroupPublic)
async def get_group(
    public_id: str,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> TelegramGroupPublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    membership = (
        await session.execute(
            select(TelegramMembership).where(
                TelegramMembership.deal_id == deal.id,
                TelegramMembership.user_id == current.user.id,
            )
        )
    ).scalar_one_or_none()
    return TelegramGroupPublic(
        status=deal.telegram_group_status,
        chat_title=deal.telegram_chat_title,
        my_membership=membership.kind.value if membership else None,
        my_invite_link=(
            membership.invite_link if membership and membership.kind is MembershipKind.INVITE else None
        ),
    )


@router.post(
    "/{public_id}/telegram-group/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Administrator: re-run a failed group creation",
)
async def retry_group(
    public_id: str,
    admin: AdminUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> dict[str, str]:
    deal = await service.get_deal_by_public_id(admin.actor, public_id)
    if deal.telegram_group_status is not TelegramGroupStatus.FAILED:
        raise InvalidTransitionError("Only a failed group creation can be retried.")
    try:
        event = await OutboxService(session).find_by_dedupe_key(f"{GROUP_EVENT}:{deal.id}")
        if event is None or not await OutboxService(session).admin_retry(event.id, now=utcnow()):
            raise ConcurrencyConflictError("The group task is not in a failed state.")
        await _reset_deal_group_status(session, deal.id)
        AuditService(session).record(
            actor=admin.actor,
            action="telegram.group_retry_requested",
            entity_type="deal",
            entity_id=deal.id,
            deal_id=deal.id,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return {"status": "queued"}


async def _reset_deal_group_status(session: AsyncSession, deal_id: int) -> None:
    deal = await session.get(Deal, deal_id)
    if deal is not None:
        deal.telegram_group_status = TelegramGroupStatus.PENDING
