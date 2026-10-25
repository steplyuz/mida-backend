"""Manual group link: an administrator creates the Telegram group in the Telegram app, then attaches it.

Telegram bots cannot create groups, so when the automatic path fails the administrator makes the group
by hand and gives its chat id here. The group worker then sees the chat is already set and does not
create a second group. One chat id can belong to one deal only.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.domain_errors import InvalidTransitionError, NotFoundError, ValidationDomainError
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.enums import TelegramGroupStatus
from app.services.audit_service import AuditService


async def link_group(
    session: AsyncSession, actor: Actor, *, deal_public_id: str, chat_id: int, title: str | None
) -> Deal:
    if chat_id >= -1_000_000_000_000:
        # Supergroup ids look like -100XXXXXXXXXX. Anything else is not a group the bot can use.
        raise ValidationDomainError("Guruh ID -100 bilan boshlanishi kerak.")
    clean_title = (title or "").strip()[:255] or None
    try:
        deal = (
            await session.execute(select(Deal).where(Deal.public_id == deal_public_id))
        ).scalar_one_or_none()
        if deal is None:
            raise NotFoundError("Deal not found.")
        if deal.telegram_group_status is TelegramGroupStatus.CREATED:
            if deal.telegram_chat_id == chat_id:
                return deal  # already linked to this chat: nothing to do
            raise InvalidTransitionError("Bu bitimga guruh allaqachon bog'langan.")
        taken = (
            await session.execute(select(Deal.id).where(Deal.telegram_chat_id == chat_id, Deal.id != deal.id))
        ).first()
        if taken is not None:
            raise InvalidTransitionError("Bu guruh boshqa bitimga bog'langan.")
        deal.telegram_chat_id = chat_id
        deal.telegram_chat_title = clean_title
        deal.telegram_group_status = TelegramGroupStatus.CREATED
        AuditService(session).record(
            actor=actor,
            action="telegram_group.linked_manually",
            entity_type="deal",
            entity_id=deal.id,
            deal_id=deal.id,
            new={"status": "created", "linked_at": utcnow().isoformat()},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return deal
