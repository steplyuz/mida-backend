"""Parses Telegram Bot API updates into a normalised, validated event.

Only group and supergroup messages are archived. Bot-authored messages are skipped, so the archive
records what people said, not what MIDA itself posted. Unknown update kinds are ignored, not errors.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.domain_errors import ValidationDomainError

GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
MEDIA_KEYS = ("photo", "document", "video", "audio", "voice", "video_note", "sticker", "animation")
SERVICE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "pinned_message",
)
MAX_NAME = 255
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ChatMessageEvent:
    kind: Literal["created", "edited"]
    chat_id: int
    message_id: int
    sender_telegram_id: int | None
    sender_display_name: str | None
    message_type: str
    text: str | None
    media: dict[str, Any] | None
    telegram_date: datetime


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationDomainError(f"Update field '{field}' must be an integer.")
    return value


def _when(seconds: object, field: str) -> datetime:
    return datetime.fromtimestamp(_int(seconds, field), UTC)


def _clean(text: str, limit: int = MAX_NAME) -> str:
    cleaned = " ".join(_CONTROL.sub(" ", unicodedata.normalize("NFC", text)).split())
    return cleaned[:limit]


def _sender(raw: dict[str, Any]) -> tuple[int | None, str | None, bool]:
    sender = raw.get("from")
    if not isinstance(sender, dict):
        return None, None, False
    is_bot = sender.get("is_bot") is True
    telegram_id = _int(sender.get("id"), "from.id") if "id" in sender else None
    name_parts = [sender.get("first_name"), sender.get("last_name")]
    display = " ".join(part for part in name_parts if isinstance(part, str) and part.strip())
    if not display and isinstance(sender.get("username"), str):
        display = "@" + sender["username"]
    return telegram_id, (_clean(display) or None), is_bot


def _media(raw: dict[str, Any]) -> dict[str, Any] | None:
    for key in MEDIA_KEYS:
        item = raw.get(key)
        if item is None:
            continue
        if isinstance(item, list):  # photos arrive as several sizes; keep the largest
            item = item[-1] if item else {}
        if not isinstance(item, dict):
            continue
        meta: dict[str, Any] = {"kind": key}
        if isinstance(item.get("file_unique_id"), str):
            meta["file_unique_id"] = item["file_unique_id"][:64]
        if isinstance(item.get("file_size"), int) and not isinstance(item.get("file_size"), bool):
            meta["file_size"] = item["file_size"]
        if isinstance(item.get("mime_type"), str):
            meta["mime_type"] = item["mime_type"][:127]
        if isinstance(item.get("file_name"), str):
            # Basename only, never a path. The name is display metadata, never a filesystem path.
            meta["file_name"] = _clean(os.path.basename(item["file_name"]))
        return meta
    return None


def parse_update(payload: object) -> tuple[int, ChatMessageEvent] | None:
    """Returns (update_id, event) for archivable group messages, or None for anything else."""
    if not isinstance(payload, dict):
        raise ValidationDomainError("Update must be a JSON object.")
    update_id = _int(payload.get("update_id"), "update_id")

    kind: Literal["created", "edited"]
    if isinstance(payload.get("message"), dict):
        raw, kind = payload["message"], "created"
    elif isinstance(payload.get("edited_message"), dict):
        raw, kind = payload["edited_message"], "edited"
    else:
        return None

    chat = raw.get("chat")
    if not isinstance(chat, dict):
        raise ValidationDomainError("Message field 'chat' must be an object.")
    if chat.get("type") not in GROUP_CHAT_TYPES:
        return None
    telegram_id, display, is_bot = _sender(raw)
    if is_bot:
        return None

    timestamp_field = "edit_date" if kind == "edited" and "edit_date" in raw else "date"
    text = raw.get("text") if isinstance(raw.get("text"), str) else raw.get("caption")
    text = text if isinstance(text, str) else None
    media = _media(raw)
    if any(key in raw for key in SERVICE_KEYS):
        message_type = "service"
        text = None
    elif media is not None:
        message_type = media["kind"]
    elif text is not None:
        message_type = "text"
    else:
        message_type = "other"

    event = ChatMessageEvent(
        kind=kind,
        chat_id=_int(chat.get("id"), "chat.id"),
        message_id=_int(raw.get("message_id"), "message_id"),
        sender_telegram_id=telegram_id,
        sender_display_name=display,
        message_type=message_type,
        text=text,
        media=media,
        telegram_date=_when(raw.get(timestamp_field), timestamp_field),
    )
    return update_id, event
