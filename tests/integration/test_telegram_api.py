"""Telegram endpoints and admin outbox endpoints, through HTTP, with a fake gateway."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.db.session import create_engine_from_url, create_session_factory
from app.main import create_app
from app.workers.outbox_worker import OutboxWorker
from app.workers.retry_policy import RetryPolicy
from tests.support.fake_telegram import FakeTelegramGateway
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

BUYER_TG, SELLER_TG, OUTSIDER_TG, ADMIN_TG = 5001, 5002, 5003, 5100


def _sqlite(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_url.removeprefix("sqlite+aiosqlite:///")))


@pytest.fixture
def fake() -> FakeTelegramGateway:
    return FakeTelegramGateway()


@pytest.fixture
def api(db_url: str, fake: FakeTelegramGateway, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        mida_logo_path=str(tmp_path / "missing-logo.png"),
    )
    with TestClient(create_app(settings, telegram_gateway=fake)) as client:
        yield client


def _login(client: TestClient, tg: int) -> dict[str, Any]:
    body = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=tg)}
    ).json()
    return {"token": body["access_token"], "user_id": body["user"]["id"]}


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def _run_worker(
    db_url: str, fake: FakeTelegramGateway, tmp_path: Path, policy: RetryPolicy | None = None
) -> None:
    async def run() -> None:
        engine = create_engine_from_url(db_url)
        try:
            worker = OutboxWorker(
                create_session_factory(engine),
                fake,
                logo_path=tmp_path / "none.png",
                invite_ttl_seconds=86_400,
                policy=policy,
            )
            await worker.run_once()
        finally:
            await engine.dispose()

    asyncio.run(run())


def _create(client: TestClient, who: dict[str, Any]) -> dict[str, Any]:
    _login(client, SELLER_TG)  # the counterparty must be a registered MIDA user
    response = client.post(
        "/api/v1/deals",
        headers={**_auth(who), "Idempotency-Key": f"tg-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "Laptop",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_group_status_is_visible_to_parties_with_their_own_invite_only(
    api: TestClient, db_url: str, fake: FakeTelegramGateway, tmp_path: Path
) -> None:
    buyer, seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    deal = _create(api, buyer)
    _run_worker(db_url, fake, tmp_path)

    buyer_view = api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_auth(buyer)).json()
    seller_view = api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_auth(seller)).json()
    assert buyer_view["status"] == seller_view["status"] == "created"
    assert buyer_view["chat_title"] == seller_view["chat_title"]
    # Each party sees only their own personal one-use link.
    assert buyer_view["my_membership"] == seller_view["my_membership"] == "invite"
    assert buyer_view["my_invite_link"] and seller_view["my_invite_link"]
    assert buyer_view["my_invite_link"] != seller_view["my_invite_link"]
    assert "chat_id" not in buyer_view


def test_outsider_cannot_see_group_status(
    api: TestClient, db_url: str, fake: FakeTelegramGateway, tmp_path: Path
) -> None:
    buyer = _login(api, BUYER_TG)
    outsider = _login(api, OUTSIDER_TG)
    deal = _create(api, buyer)
    response = api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_auth(outsider))
    assert response.status_code == 404


def test_admin_retry_requeues_only_a_failed_group(
    api: TestClient, db_url: str, fake: FakeTelegramGateway, tmp_path: Path
) -> None:
    from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError

    buyer = _login(api, BUYER_TG)
    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    deal = _create(api, buyer)

    fake.queue_failure("create", TelegramGatewayError(ErrorCategory.GROUP_CREATION))
    _run_worker(db_url, fake, tmp_path)
    assert (
        api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_auth(buyer)).json()["status"]
        == "failed"
    )

    assert (
        api.post(f"/api/v1/deals/{deal['id']}/telegram-group/retry", headers=_auth(buyer)).status_code == 403
    )
    queued = api.post(f"/api/v1/deals/{deal['id']}/telegram-group/retry", headers=_auth(admin))
    assert queued.status_code == 202

    _run_worker(db_url, fake, tmp_path)
    assert (
        api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_auth(buyer)).json()["status"]
        == "created"
    )
    assert len(fake.groups) == 1


def test_retry_of_a_healthy_group_is_refused(
    api: TestClient, db_url: str, fake: FakeTelegramGateway, tmp_path: Path
) -> None:
    buyer = _login(api, BUYER_TG)
    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    deal = _create(api, buyer)
    _run_worker(db_url, fake, tmp_path)
    response = api.post(f"/api/v1/deals/{deal['id']}/telegram-group/retry", headers=_auth(admin))
    assert response.status_code == 409


def test_admin_outbox_endpoints_are_admin_only(api: TestClient, db_url: str) -> None:
    buyer = _login(api, BUYER_TG)
    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    assert api.get("/api/v1/admin/outbox", headers=_auth(buyer)).status_code == 403
    assert api.get("/api/v1/admin/integrations/telegram", headers=_auth(buyer)).status_code == 403

    listing = api.get("/api/v1/admin/outbox", headers=_auth(admin))
    assert listing.status_code == 200
    status = api.get("/api/v1/admin/integrations/telegram", headers=_auth(admin)).json()
    assert status["telegram_configured"] is False
    assert api.post("/api/v1/admin/outbox/999999/retry", headers=_auth(admin)).status_code == 404


def test_admin_status_never_reveals_secrets(api: TestClient, db_url: str) -> None:
    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    body = api.get("/api/v1/admin/integrations/telegram", headers=_auth(admin)).text
    assert TEST_BOT_TOKEN not in body and "kkkkkkkk" not in body
