from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.domain_errors import ValidationDomainError
from app.integrations.telegram.updates import parse_update

CHAT = {"id": -1001234567890, "type": "supergroup", "title": "MIDA-000001 | x"}
HUMAN = {"id": 7001, "is_bot": False, "first_name": "Ali", "last_name": "Valiyev"}
WHEN = 1_780_000_000


def _update(message: dict[str, Any], key: str = "message") -> dict[str, Any]:
    return {"update_id": 900001, key: message}


def _msg(**extra: Any) -> dict[str, Any]:
    base = {"message_id": 55, "date": WHEN, "chat": CHAT, "from": HUMAN, "text": "Salom"}
    base.update(extra)
    return base


def test_group_text_message_is_archived() -> None:
    update_id, event = parse_update(_update(_msg()))  # type: ignore[misc]
    assert update_id == 900001
    assert event.kind == "created" and event.message_type == "text"
    assert event.text == "Salom"
    assert event.chat_id == CHAT["id"] and event.message_id == 55
    assert event.sender_telegram_id == 7001
    assert event.sender_display_name == "Ali Valiyev"
    assert event.telegram_date == datetime.fromtimestamp(WHEN, UTC)


def test_private_chats_are_not_archived() -> None:
    assert parse_update(_update({**_msg(), "chat": {"id": 1, "type": "private"}})) is None


def test_bot_authored_messages_are_skipped() -> None:
    bot = {**HUMAN, "is_bot": True}
    assert parse_update(_update(_msg(**{"from": bot}))) is None


def test_edited_message_uses_edit_date_and_is_marked_edited() -> None:
    _, event = parse_update(_update(_msg(edit_date=WHEN + 60, text="Tuzatildi"), key="edited_message"))  # type: ignore[misc]
    assert event.kind == "edited"
    assert event.telegram_date == datetime.fromtimestamp(WHEN + 60, UTC)


def test_photo_keeps_metadata_only_and_caption_as_text() -> None:
    photo = _msg(
        text=None,
        caption="Mana rasm",
        photo=[
            {"file_unique_id": "small", "file_size": 10},
            {"file_unique_id": "large", "file_size": 999, "file_id": "SECRET-FILE-ID"},
        ],
    )
    del photo["text"]
    _, event = parse_update(_update(photo))  # type: ignore[misc]
    assert event.message_type == "photo"
    assert event.text == "Mana rasm"
    assert event.media == {"kind": "photo", "file_unique_id": "large", "file_size": 999}
    assert "SECRET-FILE-ID" not in str(event.media)


def test_document_file_name_is_reduced_to_a_basename() -> None:
    doc = _msg(text=None, document={"file_unique_id": "d1", "file_name": "../../etc/passwd"})
    del doc["text"]
    _, event = parse_update(_update(doc))  # type: ignore[misc]
    assert event.media is not None and event.media["file_name"] == "passwd"


def test_service_messages_have_no_text() -> None:
    service = _msg(text=None, new_chat_members=[{"id": 9}])
    del service["text"]
    _, event = parse_update(_update(service))  # type: ignore[misc]
    assert event.message_type == "service" and event.text is None


def test_control_characters_are_stripped_from_display_names() -> None:
    sneaky = {**HUMAN, "first_name": "Ali\x00\x1b[31m", "last_name": "V\r\nal"}
    _, event = parse_update(_update(_msg(**{"from": sneaky}), key="message"))  # type: ignore[misc]
    assert event.sender_display_name is not None
    assert "\x00" not in event.sender_display_name and "\n" not in event.sender_display_name


def test_unrelated_update_kinds_are_ignored() -> None:
    assert parse_update({"update_id": 1, "callback_query": {"id": "x"}}) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        {"message": _msg()},  # missing update_id
        {"update_id": True, "message": _msg()},  # bool is not an integer
        {"update_id": 1, "message": {**_msg(), "message_id": "55"}},
        {"update_id": 1, "message": {**_msg(), "chat": "oops"}},
    ],
)
def test_malformed_updates_are_rejected(payload: object) -> None:
    with pytest.raises(ValidationDomainError):
        parse_update(payload)
