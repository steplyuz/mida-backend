from __future__ import annotations

from app.core.config import Settings
from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError
from app.integrations.telegram.gateway import TelegramGroupGateway
from app.integrations.telegram.mtproto import TelethonGateway


class UnconfiguredGateway:
    """Used when MTProto credentials are absent. Every call fails clearly, never silently."""

    _MESSAGE = (
        "Telegram MTProto is not configured: set TELEGRAM_API_ID, "
        "TELEGRAM_API_HASH and the admin session path."
    )

    async def create_supergroup(self, title: str) -> int:
        raise TelegramGatewayError(ErrorCategory.CONFIGURATION, self._MESSAGE)

    async def add_member(self, chat_id: int, telegram_user_id: int) -> None:
        raise TelegramGatewayError(ErrorCategory.CONFIGURATION, self._MESSAGE)

    async def create_invite_link(self, chat_id: int, *, usage_limit: int, expire_seconds: int) -> str:
        raise TelegramGatewayError(ErrorCategory.CONFIGURATION, self._MESSAGE)

    async def set_photo(self, chat_id: int, path: str) -> None:
        raise TelegramGatewayError(ErrorCategory.CONFIGURATION, self._MESSAGE)

    async def close(self) -> None:
        return None


def build_gateway(settings: Settings) -> TelegramGroupGateway:
    if not settings.mtproto_configured:
        return UnconfiguredGateway()
    if (
        settings.telegram_api_id is None
        or settings.telegram_api_hash is None
        or settings.telegram_admin_session_path is None
    ):
        raise ValueError("MTProto settings are incomplete")
    return TelethonGateway(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash.get_secret_value(),
        session_path=settings.telegram_admin_session_path,
    )
