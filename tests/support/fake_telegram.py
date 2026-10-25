"""Scriptable Telegram gateway fake. Records calls so tests can assert on side effects."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeTelegramGateway:
    groups: list[tuple[int, str]] = field(default_factory=list)
    members: dict[int, set[int]] = field(default_factory=dict)
    invite_links: list[tuple[int, str]] = field(default_factory=list)
    photos: list[int] = field(default_factory=list)
    script: dict[str, list[Exception]] = field(default_factory=dict)
    closed: bool = False
    _next_chat_id: int = -1001000000001
    _invite_counter: int = 0

    def queue_failure(self, operation: str, error: Exception) -> None:
        """operation: 'create', 'photo', 'invite', or 'add:<telegram_user_id>'."""
        self.script.setdefault(operation, []).append(error)

    def _maybe_fail(self, operation: str) -> None:
        queue = self.script.get(operation)
        if queue:
            raise queue.pop(0)

    async def create_supergroup(self, title: str) -> int:
        self._maybe_fail("create")
        chat_id = self._next_chat_id
        self._next_chat_id -= 1
        self.groups.append((chat_id, title))
        return chat_id

    async def add_member(self, chat_id: int, telegram_user_id: int) -> None:
        self._maybe_fail(f"add:{telegram_user_id}")
        self.members.setdefault(chat_id, set()).add(telegram_user_id)

    async def create_invite_link(self, chat_id: int, *, usage_limit: int, expire_seconds: int) -> str:
        self._maybe_fail("invite")
        self._invite_counter += 1
        link = f"https://t.me/+fake-invite-{self._invite_counter}"
        self.invite_links.append((chat_id, link))
        return link

    async def set_photo(self, chat_id: int, path: str) -> None:
        self._maybe_fail("photo")
        self.photos.append(chat_id)

    async def close(self) -> None:
        self.closed = True
