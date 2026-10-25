"""Telegram webhook and archive read endpoints, over HTTP, on a migrated SQLite file."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

SECRET = "s" * 32
CHAT = -1001777000111
BUYER_TG, SELLER_TG, OUTSIDER_TG, ADMIN_TG = 7001, 7002, 7003, 7100
HEADER = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


def _sqlite(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_url.removeprefix("sqlite+aiosqlite:///")))


@pytest.fixture
def api(db_url: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        telegram_webhook_secret=SecretStr(SECRET),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, tg: int) -> dict[str, Any]:
    body = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=tg)}
    ).json()
    return {"token": body["access_token"], "user_id": body["user"]["id"]}


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def _deal_with_chat(api: TestClient, db_url: str) -> str:
    buyer, _seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    response = api.post(
        "/api/v1/deals",
        headers={**_auth(buyer), "Idempotency-Key": f"wh-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "Camera",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    )
    assert response.status_code == 201, response.text
    public_id = response.json()["id"]
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE deals SET telegram_chat_id = ? WHERE public_id = ?", (CHAT, public_id))
    return str(public_id)


def _update(
    update_id: int, message_id: int, text: str, *, sender: int = BUYER_TG, chat: int = CHAT
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1_780_000_000,
            "chat": {"id": chat, "type": "supergroup"},
            "from": {"id": sender, "is_bot": False, "first_name": "Ali"},
            "text": text,
        },
    }


def _edit(update_id: int, message_id: int, text: str) -> dict[str, Any]:
    body = _update(update_id, message_id, text)
    body["edited_message"] = {**body.pop("message"), "edit_date": 1_780_000_100}
    return body


def test_webhook_refuses_requests_without_the_shared_secret(api: TestClient) -> None:
    body = _update(1, 1, "x")
    assert api.post("/api/v1/telegram/webhook", json=body).status_code == 401
    wrong = api.post(
        "/api/v1/telegram/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}
    )
    assert wrong.status_code == 401
    assert wrong.headers["content-type"].startswith("application/problem+json")


def test_webhook_fails_closed_when_no_secret_is_configured(db_url: str) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        database_url=db_url,  # type: ignore[call-arg]
        app_secret_key=SecretStr("k" * 32),
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/telegram/webhook", json=_update(1, 1, "x"), headers=HEADER)
    assert response.status_code == 503


def test_webhook_rejects_non_json_bodies(api: TestClient) -> None:
    response = api.post(
        "/api/v1/telegram/webhook",
        content=b"not json",
        headers={**HEADER, "content-type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:mida:error:validation-error"


def test_unsupported_and_malformed_updates_are_acknowledged_not_retried(api: TestClient) -> None:
    unsupported = api.post(
        "/api/v1/telegram/webhook", json={"update_id": 2, "callback_query": {}}, headers=HEADER
    )
    malformed = api.post(
        "/api/v1/telegram/webhook",
        json={"update_id": 3, "message": {"chat": "oops"}},
        headers=HEADER,
    )
    assert unsupported.status_code == malformed.status_code == 200
    assert unsupported.json() == malformed.json() == {"status": "ignored"}


def test_message_flows_into_the_archive_and_is_visible_to_parties_only(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    buyer, seller, outsider = (
        _login(api, BUYER_TG),
        _login(api, SELLER_TG),
        _login(api, OUTSIDER_TG),
    )
    response = api.post(
        "/api/v1/telegram/webhook",
        json=_update(100, 500, "Kamera yaxshi holatdami?"),
        headers=HEADER,
    )
    assert response.json() == {"status": "stored"}

    listing = api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(seller)).json()
    assert [m["text"] for m in listing["data"]] == ["Kamera yaxshi holatdami?"]
    assert listing["data"][0]["sender_user_id"] == buyer["user_id"]
    assert api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(outsider)).status_code == 404


def test_archive_responses_never_expose_chat_or_telegram_identifiers(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    api.post("/api/v1/telegram/webhook", json=_update(101, 501, "hi"), headers=HEADER)
    buyer = _login(api, BUYER_TG)
    body = api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(buyer)).text
    assert str(CHAT) not in body and str(BUYER_TG) not in body


def test_redelivered_update_returns_duplicate_and_adds_nothing(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    body = _update(102, 502, "once")
    first = api.post("/api/v1/telegram/webhook", json=body, headers=HEADER).json()
    second = api.post("/api/v1/telegram/webhook", json=body, headers=HEADER).json()
    assert (first["status"], second["status"]) == ("stored", "duplicate")
    buyer = _login(api, BUYER_TG)
    assert len(api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(buyer)).json()["data"]) == 1


def test_edit_history_is_exposed_to_parties(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    buyer, seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    api.post("/api/v1/telegram/webhook", json=_update(103, 503, "narx 100"), headers=HEADER)
    api.post("/api/v1/telegram/webhook", json=_edit(104, 503, "narx 90"), headers=HEADER)

    message = api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(seller)).json()["data"][0]
    assert message["text"] == "narx 90" and message["revision_count"] == 1 and message["edited_at"]
    revisions = api.get(
        f"/api/v1/deals/{public_id}/messages/{message['id']}/revisions", headers=_auth(buyer)
    ).json()
    assert [r["previous_text"] for r in revisions["data"]] == ["narx 100"]


def test_revisions_of_a_message_from_another_deal_are_not_found(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    api.post("/api/v1/telegram/webhook", json=_update(105, 504, "mine"), headers=HEADER)
    buyer = _login(api, BUYER_TG)
    message = api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(buyer)).json()["data"][0]
    other_deal = str(uuid.uuid4())
    assert (
        api.get(
            f"/api/v1/deals/{other_deal}/messages/{message['id']}/revisions", headers=_auth(buyer)
        ).status_code
        == 404
    )


def test_message_listing_paginates(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    for n in range(3):
        api.post("/api/v1/telegram/webhook", json=_update(200 + n, 600 + n, f"m{n}"), headers=HEADER)
    buyer = _login(api, BUYER_TG)
    first = api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(buyer), params={"limit": 2}).json()
    assert [m["text"] for m in first["data"]] == ["m0", "m1"] and first["pagination"]["has_more"] is True
    second = api.get(
        f"/api/v1/deals/{public_id}/messages",
        headers=_auth(buyer),
        params={"limit": 2, "cursor": first["pagination"]["next_cursor"]},
    ).json()
    assert [m["text"] for m in second["data"]] == ["m2"] and second["pagination"]["has_more"] is False


def test_admin_reads_any_archive(api: TestClient, db_url: str) -> None:
    public_id = _deal_with_chat(api, db_url)
    api.post("/api/v1/telegram/webhook", json=_update(300, 700, "for admin"), headers=HEADER)
    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    assert api.get(f"/api/v1/deals/{public_id}/messages", headers=_auth(admin)).status_code == 200
