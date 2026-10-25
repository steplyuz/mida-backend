"""User notifications: Telegram bot messages and the web app's notification list.

Enqueue side: called inside the business transaction. The outbox event (bot delivery) and the
in-app notification row are written together, so a notification exists if and only if the change
was committed. Send side: run by the outbox worker, outside any DB transaction.

The invite link is never copied into the outbox payload or the notifications table. The sender
reads it from telegram_memberships at send time, so it only ever goes to the bot.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor, ActorKind
from app.core.time import utcnow
from app.integrations.telegram.bot_api import BotClient
from app.integrations.telegram.errors import (
    ErrorCategory,
    PermanentTelegramError,
    TelegramGatewayError,
)
from app.models.deal import Deal
from app.models.enums import DealStatus
from app.models.notification import Notification
from app.models.telegram import TelegramMembership
from app.models.user import User
from app.services import bot_messages
from app.services.outbox_service import GROUP_MESSAGE_EVENT, NOTIFY_EVENT, OutboxService

logger = logging.getLogger("app.notifications")
Factory = async_sessionmaker[AsyncSession]

KIND_DEAL_CREATED = bot_messages.DEAL_CREATED
KIND_STATUS_CHANGED = bot_messages.STATUS_CHANGED
KIND_GROUP_INVITE = bot_messages.GROUP_INVITE

STATUS_TEXT: dict[str, str] = {
    DealStatus.CREATED.value: "yaratildi",
    DealStatus.AWAITING_PAYMENT.value: "to'lov kutilmoqda",
    DealStatus.PAYMENT_PENDING_VERIFICATION.value: "to'lov tekshirilmoqda",
    DealStatus.PAYMENT_CONFIRMED.value: "to'lov tasdiqlandi",
    DealStatus.DELIVERY_IN_PROGRESS.value: "yetkazib berish jarayonida",
    DealStatus.AWAITING_BUYER_CONFIRMATION.value: "xaridor tasdig'i kutilmoqda",
    DealStatus.COMPLETED.value: "yakunlandi",
    DealStatus.DISPUTED.value: "nizo ochildi",
    DealStatus.CANCELLED.value: "bekor qilindi",
    DealStatus.PAYMENT_FAILED.value: "to'lov amalga oshmadi",
    DealStatus.EXPIRED.value: "muddati tugadi",
}


async def enqueue_deal_created(session: AsyncSession, deal: Deal, creator_id: int) -> None:
    other = deal.seller_id if creator_id == deal.buyer_id else deal.buyer_id
    await _enqueue(session, deal, other, KIND_DEAL_CREATED, None)


async def enqueue_status_changed(session: AsyncSession, deal: Deal, actor: Actor, status: DealStatus) -> None:
    """Both parties, except the user who just made the change themselves."""
    skip = actor.user_id if actor.kind is ActorKind.USER else None
    for user_id in (deal.buyer_id, deal.seller_id):
        if user_id != skip:
            await _enqueue(session, deal, user_id, KIND_STATUS_CHANGED, status.value)
    # The group sees every status change too, including the one the actor made.
    if deal.telegram_chat_id is not None:
        await enqueue_group_message(session, deal, bot_messages.GROUP_STATUS, status=status.value)


async def enqueue_group_message(
    session: AsyncSession,
    deal: Deal,
    kind: str,
    *,
    status: str | None = None,
    scope: str | None = None,
) -> None:
    """Queues a message for the deal's group, sent by the group bot through the outbox (retried)."""
    dedupe_key = f"{GROUP_MESSAGE_EVENT}:{kind}:{deal.id}:{scope or f'v{deal.version}'}"
    outbox = OutboxService(session)
    if await outbox.find_by_dedupe_key(dedupe_key) is not None:
        return
    payload: dict[str, Any] = {"deal_id": deal.id, "kind": kind}
    if status is not None:
        payload["status"] = status
    await outbox.enqueue(
        event_type=GROUP_MESSAGE_EVENT,
        aggregate_id=deal.id,
        dedupe_key=dedupe_key,
        payload=payload,
        now=utcnow(),
    )


def _group_fields(kind: str, deal: Deal, status: str | None) -> dict[str, str]:
    fields = {"deal_number": deal.deal_number, "title": deal.title}
    if kind == bot_messages.GROUP_STATUS or kind == bot_messages.GROUP_HOLAT:
        fields["status"] = STATUS_TEXT.get(status or "", status or "yangilandi")
    if kind == bot_messages.GROUP_RULES:
        fields["terms"] = _terms_text(deal)
    return fields


def _terms_text(deal: Deal) -> str:
    try:
        terms = json.loads(deal.terms_snapshot_json).get("terms_text", "")
    except (ValueError, AttributeError):
        terms = ""
    text = str(terms).strip() or "Shartlar kiritilmagan."
    return text[:2500]


async def send_group_message(session_factory: Factory, bot: BotClient, payload: dict[str, Any]) -> None:
    """Runs in the outbox worker. The text is rendered from current deal data at send time."""
    deal_id, kind = payload.get("deal_id"), payload.get("kind")
    if not isinstance(deal_id, int) or not isinstance(kind, str):
        raise PermanentTelegramError("bad_payload")
    status = payload.get("status")
    async with session_factory() as session:
        deal = await session.get(Deal, deal_id)
        if deal is None:
            raise PermanentTelegramError("deal_not_found")
        if deal.telegram_chat_id is None:
            raise PermanentTelegramError("group_not_created")
        text = bot_messages.render(
            kind,
            _group_fields(kind, deal, status if isinstance(status, str) else None),
        )
        chat_id = deal.telegram_chat_id
    try:
        await bot.send_message(chat_id, text)
    except TelegramGatewayError as exc:
        if exc.category is ErrorCategory.USER_UNREACHABLE:
            # Telegram answers 403 when the bot is not (or no longer) a member of the group.
            raise PermanentTelegramError("bot_not_in_group") from None
        raise


async def enqueue_group_invite(session: AsyncSession, deal: Deal, user_id: int) -> None:
    """Sends one party their personal one-use invite link to the deal's group."""
    await _enqueue(session, deal, user_id, KIND_GROUP_INVITE, None, dedupe_scope="group")


def _fields(kind: str, deal: Deal, status: str | None, *, is_buyer: bool) -> dict[str, str]:
    fields = {"deal_number": deal.deal_number, "title": deal.title}
    if kind == KIND_DEAL_CREATED:
        fields["role"] = "xaridor" if is_buyer else "sotuvchi"
    elif kind == KIND_STATUS_CHANGED:
        fields["status"] = STATUS_TEXT.get(status or "", status or "yangilandi")
    elif kind == KIND_GROUP_INVITE:
        fields["chat_title"] = deal.telegram_chat_title or deal.deal_number
    return fields


async def _enqueue(
    session: AsyncSession,
    deal: Deal,
    user_id: int,
    kind: str,
    status: str | None,
    *,
    dedupe_scope: str | None = None,
) -> None:
    scope = dedupe_scope or f"v{deal.version}"
    dedupe_key = f"{NOTIFY_EVENT}:{kind}:{deal.id}:{scope}:{user_id}"
    outbox = OutboxService(session)
    if await outbox.find_by_dedupe_key(dedupe_key) is not None:
        return  # Already queued. Do not add a second in-app row either.

    payload: dict[str, Any] = {"deal_id": deal.id, "user_id": user_id, "kind": kind}
    if status is not None:
        payload["status"] = status
    await outbox.enqueue(
        event_type=NOTIFY_EVENT,
        aggregate_id=deal.id,
        dedupe_key=dedupe_key,
        payload=payload,
        now=utcnow(),
    )
    # In-app text never includes the invite link: it renders the pointer to the deal page instead.
    fields = _fields(kind, deal, status, is_buyer=user_id == deal.buyer_id)
    session.add(
        Notification(
            user_id=user_id,
            deal_id=deal.id,
            kind=kind,
            body=bot_messages.render(kind, fields, link=None),
            created_at=utcnow(),
            read_at=None,
        )
    )


class NotificationSender:
    def __init__(self, session_factory: Factory, bot: BotClient, *, miniapp_url: str | None) -> None:
        self._sf = session_factory
        self._bot = bot
        self._url = miniapp_url

    async def send(self, payload: dict[str, Any]) -> None:
        deal_id, user_id = payload.get("deal_id"), payload.get("user_id")
        kind = payload.get("kind")
        if not isinstance(deal_id, int) or not isinstance(user_id, int) or not isinstance(kind, str):
            raise PermanentTelegramError("bad_payload")
        status = payload.get("status")
        async with self._sf() as session:
            deal = await session.get(Deal, deal_id)
            user = await session.get(User, user_id)
            if deal is None or user is None:
                raise PermanentTelegramError("deal_or_user_not_found")
            if not user.is_active:
                return
            link: str | None = None
            if kind == KIND_GROUP_INVITE:
                membership = (
                    await session.execute(
                        select(TelegramMembership).where(
                            TelegramMembership.deal_id == deal.id,
                            TelegramMembership.user_id == user.id,
                        )
                    )
                ).scalar_one_or_none()
                link = membership.invite_link if membership else None
                if link is None:
                    raise PermanentTelegramError("invite_not_found")
            fields = _fields(
                kind,
                deal,
                status if isinstance(status, str) else None,
                is_buyer=user.id == deal.buyer_id,
            )
            text = bot_messages.render(kind, fields, link=link)
            chat_id = user.telegram_user_id

        try:
            await self._bot.send_message(
                chat_id,
                text,
                button_text="Bitimni ochish" if self._url else None,
                button_url=f"{self._url.rstrip('/')}/deals/{deal.public_id}" if self._url else None,
            )
        except TelegramGatewayError as exc:
            if exc.category is ErrorCategory.USER_UNREACHABLE:
                # The user has not started the bot or blocked it: skip, do not dead-letter.
                # The web app notification still exists, so nothing is lost.
                logger.info(
                    "notification skipped",
                    extra={"operation": NOTIFY_EVENT, "deal_id": deal_id, "result": "unreachable"},
                )
                return
            raise


def render_text(kind: str, deal: Deal, status: str | None, *, is_buyer: bool) -> str:
    """Bot text without a link, for previews and tests."""
    return bot_messages.render(kind, _fields(kind, deal, status, is_buyer=is_buyer), link=None)
