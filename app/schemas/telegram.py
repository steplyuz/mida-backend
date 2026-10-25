from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import TelegramGroupStatus


class TelegramGroupPublic(BaseModel):
    """What a party may see. The chat id is internal and never returned."""

    status: TelegramGroupStatus
    chat_title: str | None
    my_invite_link: str | None
    my_membership: str | None


class OutboxEventAdmin(BaseModel):
    id: int
    event_type: str
    aggregate_id: int
    status: str
    attempt_count: int
    last_error_code: str | None
    next_attempt_at: datetime
    created_at: datetime
    processed_at: datetime | None


class OutboxListAdmin(BaseModel):
    data: list[OutboxEventAdmin]


class IntegrationStatusAdmin(BaseModel):
    telegram_configured: bool
    worker_enabled: bool
    outbox_counts: dict[str, int]
