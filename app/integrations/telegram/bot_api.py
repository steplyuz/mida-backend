"""Telegram Bot API client used for user notifications (sendMessage only).

The bot token is part of the request URL, so exception text from httpx is never forwarded or
logged: errors are mapped to ErrorCategory and carry no URL.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_TEXT_LENGTH = 4096


class BotClient(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


class UnconfiguredBot:
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> None:
        raise TelegramGatewayError(ErrorCategory.CONFIGURATION, "TELEGRAM_BOT_TOKEN is not configured.")

    async def close(self) -> None:
        return None


class BotApiClient:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = f"{API_BASE}/bot{token}/sendMessage"
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT_LENGTH],
            "disable_web_page_preview": True,
        }
        if button_text and button_url:
            body["reply_markup"] = {
                "inline_keyboard": [[{"text": button_text, "web_app": {"url": button_url}}]]
            }
        try:
            response = await self._client.post(self._url, json=body)
        except (httpx.TransportError, httpx.TimeoutException):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None

        if response.status_code == 200:
            return
        retry_after: int | None = None
        description = ""
        try:
            data = response.json()
            description = str(data.get("description", "")).lower()
            retry_after = (data.get("parameters") or {}).get("retry_after")
        except ValueError:
            pass

        status = response.status_code
        if status == 429:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=retry_after or 5)
        if status == 403 or (status == 400 and "chat not found" in description):
            # The user blocked the bot or never pressed Start. Nothing a retry can fix.
            raise TelegramGatewayError(ErrorCategory.USER_UNREACHABLE)
        if status in (401, 404):
            raise TelegramGatewayError(ErrorCategory.AUTHENTICATION, "Bot token was rejected.")
        if status >= 500:
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK)
        raise TelegramGatewayError(ErrorCategory.NOTIFICATION, f"sendMessage returned {status}.")

    async def close(self) -> None:
        await self._client.aclose()
