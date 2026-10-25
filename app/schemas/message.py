"""Archived message wire format. Chat ids, Telegram user ids and file ids are not exposed."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.message import DealMessage, MessageRevision


class MessagePublic(BaseModel):
    id: int
    message_type: str
    text: str | None
    sender_user_id: int | None
    sender_display_name: str | None
    media: dict[str, Any] | None
    telegram_date: datetime
    recorded_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    is_deleted: bool
    revision_count: int

    @classmethod
    def build(cls, message: DealMessage) -> MessagePublic:
        return cls(
            id=message.id,
            message_type=message.message_type,
            text=message.text_content,
            sender_user_id=message.sender_user_id,
            sender_display_name=message.sender_display_name,
            media=json.loads(message.media_metadata_json) if message.media_metadata_json else None,
            telegram_date=message.telegram_message_date,
            recorded_at=message.recorded_at,
            edited_at=message.edited_at,
            deleted_at=message.deleted_at,
            is_deleted=message.is_deleted,
            revision_count=message.revision_count,
        )


class MessagePage(BaseModel):
    data: list[MessagePublic]
    pagination: dict[str, Any]


class RevisionPublic(BaseModel):
    revision_no: int
    change_kind: str
    previous_text: str | None
    recorded_at: datetime

    @classmethod
    def build(cls, revision: MessageRevision) -> RevisionPublic:
        return cls(
            revision_no=revision.revision_no,
            change_kind=revision.change_kind.value,
            previous_text=revision.previous_text,
            recorded_at=revision.recorded_at,
        )


class RevisionPage(BaseModel):
    data: list[RevisionPublic]
