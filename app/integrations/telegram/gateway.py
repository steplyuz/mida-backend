"""The only surface the app uses to reach Telegram: MTProto in production, a fake in tests."""

from __future__ import annotations

from typing import Protocol


class TelegramGroupGateway(Protocol):
    async def create_supergroup(self, title: str) -> int:
        """Creates a supergroup owned by the MIDA admin session. Returns the chat id."""

    async def add_member(self, chat_id: int, telegram_user_id: int) -> None:
        """Adds a user. Adding a user who is already a member must succeed silently."""

    async def create_invite_link(self, chat_id: int, *, usage_limit: int, expire_seconds: int) -> str:
        """Creates a link with a usage limit and expiry."""

    async def set_photo(self, chat_id: int, path: str) -> None:
        """Sets the group photo from a local image file."""

    async def close(self) -> None:
        """Releases network resources."""
