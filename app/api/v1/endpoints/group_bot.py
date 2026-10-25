"""Webhook of the group bot (separate from the notification bot). Fails closed without a secret."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request

from app.core.config import Settings
from app.core.domain_errors import (
    AuthenticationError,
    ServiceNotConfiguredError,
    ValidationDomainError,
)
from app.services.group_bot_service import GroupBotService

router = APIRouter(prefix="/telegram/group-bot", tags=["telegram"])
logger = logging.getLogger("app.group_bot")


@router.post("/webhook", summary="Group bot update receiver")
async def receive_group_update(
    request: Request,
    secret_token: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    if settings.group_bot_webhook_secret is None:
        raise ServiceNotConfiguredError("The group bot webhook is not configured.")
    expected = settings.group_bot_webhook_secret.get_secret_value().encode()
    if secret_token is None or not secrets.compare_digest(secret_token.encode(), expected):
        raise AuthenticationError("Invalid webhook secret.")

    try:
        payload: Any = await request.json()
    except ValueError:
        raise ValidationDomainError("Webhook body must be JSON.") from None
    if not isinstance(payload, dict):
        return {"status": "ignored"}

    service = GroupBotService(
        request.app.state.session_factory,
        request.app.state.group_bot,
        username=settings.group_bot_username,
    )
    outcome = await service.handle_update(payload)
    logger.info("group bot update", extra={"operation": "group_bot", "result": outcome})
    return {"status": outcome}
