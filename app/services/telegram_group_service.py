"""Creates a deal's group, adds both parties (or issues one-use invite links), and sets the logo.

Each step commits its outcome before the next one runs, so a retry resumes where it stopped and
never creates a second group. Telegram calls happen outside any open DB transaction.

Known residual risk: if the process dies between the Telegram call that creates the group and the
commit that stores its chat id, that group is orphaned. 'telegram.group_create_started' is audited
before the call, so operators can reconcile it. MTProto offers no idempotency key to close this gap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.time import utcnow
from app.integrations.telegram.errors import (
    PermanentTelegramError,
)
from app.integrations.telegram.gateway import TelegramGroupGateway
from app.models.deal import Deal
from app.models.enums import MembershipKind, TelegramGroupStatus
from app.models.telegram import TelegramMembership
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService
from app.services.notification_service import enqueue_group_invite
from app.services.outbox_service import AVATAR_EVENT, OutboxService
from app.services.telegram_group_title import build_group_title

logger = logging.getLogger("app.telegram")
Factory = async_sessionmaker[AsyncSession]
MAX_LOGO_BYTES = 5_000_000
ALLOWED_LOGO_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})


def logo_is_usable(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in ALLOWED_LOGO_SUFFIXES
        and path.stat().st_size <= MAX_LOGO_BYTES
    )


class TelegramGroupService:
    def __init__(
        self,
        session_factory: Factory,
        gateway: TelegramGroupGateway,
        *,
        logo_path: Path,
        invite_ttl_seconds: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sf = session_factory
        self._gateway = gateway
        self._logo = logo_path
        self._invite_ttl = invite_ttl_seconds
        self._clock = clock

    async def create_group(self, deal_id: int) -> None:
        chat_id = await self._ensure_group(deal_id)
        await self._ensure_members(deal_id, chat_id)

    async def set_avatar(self, deal_id: int) -> str:
        async with self._sf() as session:
            deal = await session.get(Deal, deal_id)
            if deal is None:
                raise PermanentTelegramError("deal_not_found")
            chat_id = deal.telegram_chat_id
        if chat_id is None:
            raise PermanentTelegramError("group_not_created")
        if not logo_is_usable(self._logo):
            return "skipped"
        await self._gateway.set_photo(chat_id, str(self._logo))
        return "done"

    async def mark_group_failed(self, deal_id: int, code: str) -> None:
        async with self._sf() as session:
            deal = await session.get(Deal, deal_id)
            if deal is None or deal.telegram_group_status is TelegramGroupStatus.CREATED:
                return
            deal.telegram_group_status = TelegramGroupStatus.FAILED
            AuditService(session).record(
                actor=Actor.system(),
                action="telegram.group_failed",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={"error_code": code},
            )
            await session.commit()

    async def _ensure_group(self, deal_id: int) -> int:
        async with self._sf() as session:
            deal = await session.get(Deal, deal_id)
            if deal is None:
                raise PermanentTelegramError("deal_not_found")
            if deal.telegram_chat_id is not None:
                return deal.telegram_chat_id
            title = build_group_title(deal.deal_number, deal.title)
            deal.telegram_group_status = TelegramGroupStatus.PENDING
            AuditService(session).record(
                actor=Actor.system(),
                action="telegram.group_create_started",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={"title": title},
            )
            await session.commit()

        # Outside any transaction: the Telegram call may take seconds.
        chat_id = await self._gateway.create_supergroup(title)

        async with self._sf() as session:
            deal = await session.get(Deal, deal_id)
            if deal is None:
                raise PermanentTelegramError("deal_not_found")
            deal.telegram_chat_id = chat_id
            deal.telegram_chat_title = title
            deal.telegram_group_status = TelegramGroupStatus.CREATED
            AuditService(session).record(
                actor=Actor.system(),
                action="telegram.group_created",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={"title": title},
            )
            await OutboxService(session).enqueue(
                event_type=AVATAR_EVENT,
                aggregate_id=deal.id,
                dedupe_key=f"{AVATAR_EVENT}:{deal.id}",
                payload={"deal_id": deal.id},
                now=self._clock(),
            )
            await session.commit()
        logger.info("telegram group created", extra={"operation": "group_create", "deal_id": deal_id})
        return chat_id

    async def _ensure_members(self, deal_id: int, chat_id: int) -> None:
        async with self._sf() as session:
            parties = await DealRepository(session).list_participants(deal_id)
            done = {
                row.user_id
                for row in (
                    await session.execute(
                        select(TelegramMembership).where(TelegramMembership.deal_id == deal_id)
                    )
                ).scalars()
            }
            pending = [
                (user.id, user.telegram_user_id, participant.participant_role)
                for participant, user in parties
                if user.id not in done
            ]

        for user_id, _telegram_user_id, role in pending:
            link = await self._create_personal_invite(chat_id)
            async with self._sf() as session:
                session.add(
                    TelegramMembership(
                        deal_id=deal_id,
                        user_id=user_id,
                        kind=MembershipKind.INVITE,
                        invite_link=link,
                        created_at=self._clock(),
                    )
                )
                # Sends this party their personal link. The bot message carries it; the web app
                # shows the same link on the deal page.
                deal = await session.get(Deal, deal_id)
                if deal is not None:
                    await enqueue_group_invite(session, deal, user_id)
                await session.commit()
            # Deliberately logs the role and kind only. Never the link or the Telegram id.
            logger.info(
                "telegram party joined",
                extra={
                    "operation": "add_member",
                    "deal_id": deal_id,
                    "role": role.value,
                    "result": MembershipKind.INVITE.value,
                },
            )

    async def _create_personal_invite(self, chat_id: int) -> str:
        """Every party gets their own one-use link. Nobody is added to the group directly."""
        return await self._gateway.create_invite_link(chat_id, usage_limit=1, expire_seconds=self._invite_ttl)
