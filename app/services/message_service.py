"""Message archive: ingests group messages, applies edits and deletions as revisions, and serves
history to deal parties. Access checks happen in the caller (DealService), not here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow
from app.integrations.telegram.updates import ChatMessageEvent
from app.models.deal import Deal, DealParticipant
from app.models.enums import MessageSource, RevisionKind
from app.models.message import DealMessage, MessageRevision, TelegramUpdateReceipt
from app.models.user import User

Outcome = Literal["stored", "edited", "unchanged", "duplicate", "deleted", "ignored_unknown_chat"]


@dataclass(frozen=True, slots=True)
class PageResult:
    rows: list[DealMessage]
    has_more: bool


class MessageService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest(self, update_id: int, event: ChatMessageEvent) -> Outcome:
        """Records the update and applies it in one transaction. Any failure rolls both back, so a
        redelivered update is processed again instead of being silently lost."""
        moment = utcnow()
        try:
            self._session.add(TelegramUpdateReceipt(update_id=update_id, received_at=moment))
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return "duplicate"

            deal = await self._deal_for_chat(event.chat_id)
            if deal is None:
                await self._session.commit()  # the receipt still stops redelivery loops
                return "ignored_unknown_chat"

            outcome = await self._apply(deal, event, moment)
            await self._session.commit()
            return outcome
        except Exception:
            await self._session.rollback()
            raise

    async def apply_deletion(self, chat_id: int, message_id: int, *, at: datetime | None = None) -> Outcome:
        """Deletions do not arrive via the Bot API. This path serves a future MTProto source and
        is exercised directly by tests. The text is kept; the deletion is recorded as a revision."""
        moment = at or utcnow()
        try:
            message = await self._find(chat_id, message_id)
            if message is None:
                return "ignored_unknown_chat"
            if message.is_deleted:
                return "unchanged"
            await self._add_revision(message, RevisionKind.DELETED, message.text_content, moment)
            message.is_deleted = True
            message.deleted_at = moment
            await self._session.commit()
            return "deleted"
        except Exception:
            await self._session.rollback()
            raise

    async def list_messages(self, deal_id: int, *, after_id: int | None, limit: int) -> PageResult:
        stmt = select(DealMessage).where(DealMessage.deal_id == deal_id)
        if after_id is not None:
            stmt = stmt.where(DealMessage.id > after_id)
        rows = list(
            (await self._session.execute(stmt.order_by(DealMessage.id).limit(limit + 1))).scalars().all()
        )
        return PageResult(rows=rows[:limit], has_more=len(rows) > limit)

    async def list_revisions(self, deal_id: int, message_id: int) -> list[MessageRevision] | None:
        message = await self._session.get(DealMessage, message_id)
        if message is None or message.deal_id != deal_id:
            return None
        stmt = (
            select(MessageRevision)
            .where(MessageRevision.message_id == message_id)
            .order_by(MessageRevision.revision_no)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ---------- internals ----------

    async def _deal_for_chat(self, chat_id: int) -> Deal | None:
        return (
            await self._session.execute(select(Deal).where(Deal.telegram_chat_id == chat_id))
        ).scalar_one_or_none()

    async def _find(self, chat_id: int, message_id: int) -> DealMessage | None:
        return (
            await self._session.execute(
                select(DealMessage).where(
                    DealMessage.telegram_chat_id == chat_id,
                    DealMessage.telegram_message_id == message_id,
                )
            )
        ).scalar_one_or_none()

    async def _apply(self, deal: Deal, event: ChatMessageEvent, moment: datetime) -> Outcome:
        existing = await self._find(event.chat_id, event.message_id)
        if existing is None:
            self._session.add(await self._new_message(deal, event, moment))
            await self._session.flush()
            return "stored"
        if event.kind == "created":
            return "duplicate"
        if existing.text_content == event.text:
            return "unchanged"
        await self._add_revision(existing, RevisionKind.EDITED, existing.text_content, moment)
        existing.text_content = event.text
        existing.message_type = event.message_type
        existing.edited_at = event.telegram_date
        await self._session.flush()
        return "edited"

    async def _new_message(self, deal: Deal, event: ChatMessageEvent, moment: datetime) -> DealMessage:
        sender_user_id = None
        if event.sender_telegram_id is not None:
            sender_user_id = (
                await self._session.execute(
                    select(DealParticipant.user_id)
                    .join(User, User.id == DealParticipant.user_id)
                    .where(
                        DealParticipant.deal_id == deal.id,
                        User.telegram_user_id == event.sender_telegram_id,
                    )
                )
            ).scalar_one_or_none()
        return DealMessage(
            deal_id=deal.id,
            source=MessageSource.BOT_WEBHOOK,
            telegram_chat_id=event.chat_id,
            telegram_message_id=event.message_id,
            sender_user_id=sender_user_id,
            sender_telegram_id=event.sender_telegram_id,
            sender_display_name=event.sender_display_name,
            message_type=event.message_type,
            text_content=event.text,
            media_metadata_json=json.dumps(event.media, sort_keys=True) if event.media else None,
            telegram_message_date=event.telegram_date,
            recorded_at=moment,
            edited_at=event.telegram_date if event.kind == "edited" else None,
            deleted_at=None,
            is_deleted=False,
            revision_count=0,
        )

    async def _add_revision(
        self, message: DealMessage, kind: RevisionKind, previous: str | None, moment: datetime
    ) -> None:
        count = await self._session.scalar(
            select(func.count()).select_from(MessageRevision).where(MessageRevision.message_id == message.id)
        )
        number = int(count or 0) + 1
        self._session.add(
            MessageRevision(
                message_id=message.id,
                revision_no=number,
                change_kind=kind,
                previous_text=previous,
                recorded_at=moment,
            )
        )
        message.revision_count = number
        await self._session.flush()
