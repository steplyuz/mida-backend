"""The group bot: posts rules when it joins a deal group, answers commands from the two parties.

Security rules this module keeps:
- Only the buyer and seller of the deal get answers. Anyone else in the group gets silence, so the
  bot does not reveal which deals exist or who is in them.
- Personal invite links are never posted in the group. /havola sends the link in a private chat.
- Replies to the group contain no links, no amounts and no user ids.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.telegram.bot_api import BotClient
from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError
from app.models.deal import Deal
from app.models.enums import MembershipKind
from app.models.telegram import TelegramMembership
from app.models.user import User
from app.services import bot_messages
from app.services.notification_service import STATUS_TEXT, enqueue_group_message

logger = logging.getLogger("app.group_bot")
Factory = async_sessionmaker[AsyncSession]
GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
ACTIVE_MEMBER_STATUSES = frozenset({"member", "administrator"})


class GroupBotService:
    def __init__(self, session_factory: Factory, bot: BotClient, *, username: str | None) -> None:
        self._sf = session_factory
        self._bot = bot
        self._username = username.lower().lstrip("@") if username else None

    async def handle_update(self, update: dict[str, Any]) -> str:
        if isinstance(update.get("my_chat_member"), dict):
            return await self._on_membership(update["my_chat_member"])
        if isinstance(update.get("message"), dict):
            return await self._on_message(update["message"])
        return "ignored"

    # --- the bot was added to a group -------------------------------------------------

    async def _on_membership(self, change: dict[str, Any]) -> str:
        chat = change.get("chat") or {}
        new_member = change.get("new_chat_member") or {}
        if chat.get("type") not in GROUP_CHAT_TYPES:
            return "ignored"
        if new_member.get("status") not in ACTIVE_MEMBER_STATUSES:
            return "ignored"
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return "ignored"
        async with self._sf() as session:
            deal = await self._deal_for_chat(session, chat_id)
            if deal is None:
                return "ignored"
            # Keyed by the deal, not the event: adding the bot twice posts the rules once.
            await enqueue_group_message(session, deal, bot_messages.GROUP_RULES, scope="rules")
            await session.commit()
        return "rules_queued"

    # --- commands ----------------------------------------------------------------------

    async def _on_message(self, message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = message.get("text")
        if sender.get("is_bot") or not isinstance(text, str) or not text.startswith("/"):
            return "ignored"
        command, addressed_to = _parse_command(text)
        if addressed_to is not None and self._username and addressed_to != self._username:
            return "ignored"  # addressed to another bot in the same group

        if chat.get("type") == "private":
            return await self._private(command, chat)
        if chat.get("type") not in GROUP_CHAT_TYPES:
            return "ignored"
        if command not in {"holat", "havola", "yordam"}:
            return "ignored"

        telegram_user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(telegram_user_id, int) or not isinstance(chat_id, int):
            return "ignored"

        async with self._sf() as session:
            deal = await self._deal_for_chat(session, chat_id)
            if deal is None:
                return "ignored"
            user = await _user_by_telegram(session, telegram_user_id)
            if user is None or user.id not in (deal.buyer_id, deal.seller_id):
                return "ignored"  # silence for everyone who is not a party
            status_text = STATUS_TEXT.get(deal.status.value, deal.status.value)
            holat_fields = {
                "deal_number": deal.deal_number,
                "title": deal.title,
                "status": status_text,
            }
            link: str | None = None
            if command == "havola":
                membership = await _membership(session, deal.id, user.id)
                if membership is not None and membership.kind is MembershipKind.INVITE:
                    link = membership.invite_link
            private_fields = {"deal_number": deal.deal_number, "title": deal.title}
            telegram_id = user.telegram_user_id

        if command == "yordam":
            return await self._send(chat_id, bot_messages.render(bot_messages.GROUP_HELP, {}))

        if command == "holat":
            return await self._send(chat_id, bot_messages.render(bot_messages.GROUP_HOLAT, holat_fields))
        # command == "havola": the link itself goes only to the private chat.
        if not link:
            return await self._send(chat_id, "Sizning shaxsiy havolangiz topilmadi. Admin bilan bog'laning.")
        private_text = bot_messages.render(
            bot_messages.GROUP_HAVOLA_PRIVATE, {**private_fields, "link": link}
        )
        try:
            await self._bot.send_message(telegram_id, private_text)
        except TelegramGatewayError as exc:
            if exc.category is ErrorCategory.USER_UNREACHABLE:
                return await self._send(
                    chat_id, "Havolani shaxsiy xabarda yuborish uchun avval botga /start yozing."
                )
            raise
        return await self._send(chat_id, "Havola shaxsiy xabaringizga yuborildi.")

    async def _private(self, command: str, chat: dict[str, Any]) -> str:
        """Private chat /start: explains the bot. Needed once, so the bot may send /havola links."""
        if command != "start":
            return "ignored"
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return "ignored"
        return await self._send(
            chat_id,
            "Assalomu alaykum. Bu bot MIDA bitim guruhlarida ishlaydi. "
            "Guruhda /holat, /havola yoki /yordam buyruqlarini yozing.",
        )

    async def _send(self, chat_id: int, text: str) -> str:
        try:
            await self._bot.send_message(chat_id, text)
        except TelegramGatewayError as exc:
            logger.info(
                "group bot reply failed",
                extra={"operation": "group_bot", "result": exc.category.value},
            )
            return "send_failed"
        return "replied"

    async def _deal_for_chat(self, session: AsyncSession, chat_id: int) -> Deal | None:
        return (
            await session.execute(select(Deal).where(Deal.telegram_chat_id == chat_id))
        ).scalar_one_or_none()


def _parse_command(text: str) -> tuple[str, str | None]:
    """'/holat@MidaBot extra' -> ('holat', 'midabot'). Lower-cased; bot name optional."""
    head = text.split()[0] if text.split() else ""
    name, _, addressed = head[1:].partition("@")
    return name.lower(), (addressed.lower() or None)


async def _user_by_telegram(session: AsyncSession, telegram_user_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one_or_none()


async def _membership(session: AsyncSession, deal_id: int, user_id: int) -> TelegramMembership | None:
    return (
        await session.execute(
            select(TelegramMembership).where(
                TelegramMembership.deal_id == deal_id, TelegramMembership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
