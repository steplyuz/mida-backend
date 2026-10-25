"""Errors raised by Telegram integration. Categories decide retry behaviour in the worker."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TEMPORARY_NETWORK = "temporary_network"
    INVALID_PARTICIPANT = "invalid_participant"
    GROUP_CREATION = "group_creation"
    AVATAR = "avatar"
    USER_UNREACHABLE = "user_unreachable"
    NOTIFICATION = "notification"


RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.RATE_LIMIT, ErrorCategory.TEMPORARY_NETWORK, ErrorCategory.AVATAR}
)


class TelegramGatewayError(Exception):
    """A Telegram call failed. The message is for operators; it must never contain secrets."""

    def __init__(self, category: ErrorCategory, message: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(message or category.value)
        self.category = category
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


class PermanentTelegramError(Exception):
    """Non-retryable failure of a worker task (bad data, missing deal, unknown event)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
