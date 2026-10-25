"""Scriptable Bot API fake for notification tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.integrations.telegram.errors import TelegramGatewayError


@dataclass
class FakeBot:
    sent: list[tuple[int, str, str | None]] = field(default_factory=list)
    failures: dict[int, TelegramGatewayError] = field(default_factory=dict)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> None:
        if chat_id in self.failures:
            raise self.failures[chat_id]
        self.sent.append((chat_id, text, button_url))

    async def close(self) -> None:
        return None
