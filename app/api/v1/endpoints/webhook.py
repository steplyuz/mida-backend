"""Telegram Bot API webhook. Authenticated by a shared secret header, never by user tokens."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.api.deps import SessionDep
from app.core.config import Settings
from app.core.domain_errors import (
    AuthenticationError,
    ServiceNotConfiguredError,
    ValidationDomainError,
)
from app.integrations.telegram.updates import parse_update
from app.services.message_service import MessageService

router = APIRouter(prefix="/telegram", tags=["telegram"])
logger = logging.getLogger("app.webhook")


@router.post("/webhook", summary="Telegram Bot API update receiver")
async def receive_update(
    request: Request,
    session: SessionDep,
    secret_token: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    if settings.telegram_webhook_secret is None:
        raise ServiceNotConfiguredError("The Telegram webhook is not configured.")
    expected = settings.telegram_webhook_secret.get_secret_value()
    if secret_token is None or not secrets.compare_digest(secret_token.encode(), expected.encode()):
        raise AuthenticationError("Invalid webhook secret.")

    try:
        payload = await request.json()
    except ValueError:
        raise ValidationDomainError("Webhook body must be JSON.") from None

    try:
        parsed = parse_update(payload)
    except ValidationDomainError:
        # Malformed updates are acknowledged so Telegram does not retry them forever.
        logger.warning(
            "malformed telegram update ignored", extra={"operation": "webhook", "result": "ignored"}
        )
        return {"status": "ignored"}

    if parsed is None:
        return {"status": "ignored"}
    update_id, event = parsed
    outcome = await MessageService(session).ingest(update_id, event)
    logger.info("telegram update processed", extra={"operation": "webhook", "result": outcome})
    return {"status": outcome}
