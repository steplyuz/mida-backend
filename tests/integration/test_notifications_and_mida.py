"""Random MIDA IDs, group links sent to the bot, and the web app's notification list."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.config import Settings
from app.db.session import create_engine_from_url, create_session_factory
from app.main import create_app
from app.models.deal import Deal
from app.models.notification import Notification
from app.models.telegram import TelegramMembership
from app.services.deal_service import CreateDealInput, DealService
from app.services.pricing_service import PricingService
from app.workers.outbox_worker import OutboxWorker
from tests.support.fake_bot import FakeBot
from tests.support.fake_telegram import FakeTelegramGateway
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

Factory = async_sessionmaker[AsyncSession]
APP_SECRET = "k" * 32
BUYER_TG, SELLER_TG = 7101, 7102


# ---------- MIDA ID ----------


@pytest.fixture
def api(db_url: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr(APP_SECRET),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, telegram_user_id: int) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/telegram",
        json={
            "init_data": valid_init_data(telegram_user_id=telegram_user_id, first_name=f"U{telegram_user_id}")
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return {"token": body["access_token"], "user_id": body["user"]["id"], "body": body}


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def test_mida_ids_are_random_eight_digits_and_not_sequential(api: TestClient) -> None:
    ids = [_login(api, 8000 + n)["body"]["user"]["mida_id"] for n in range(8)]
    assert all(isinstance(i, int) and 10_000_000 <= i <= 99_999_999 for i in ids), ids
    assert len(set(ids)) == len(ids)
    # Sequential numbering would produce consecutive values; random ones essentially never do.
    assert not all(b - a == 1 for a, b in zip(ids, ids[1:], strict=False))


def test_login_again_keeps_the_same_mida_id(api: TestClient) -> None:
    first = _login(api, 8100)["body"]["user"]["mida_id"]
    second = _login(api, 8100)["body"]["user"]["mida_id"]
    assert first == second


# ---------- Group links reach the bot ----------


async def _worker_run(sf: Factory, fake: FakeTelegramGateway, bot: FakeBot, logo: Path) -> None:
    worker = OutboxWorker(
        sf,
        fake,
        logo_path=logo,
        invite_ttl_seconds=60,
        bot=bot,
        miniapp_url="https://app.example",
    )
    for _ in range(4):  # group, then notifications queued by the group step
        await worker.run_once()


async def test_invited_party_gets_the_one_use_link_in_the_bot(
    session_factory: Factory, pricing: PricingService, make_user: Any, tmp_path: Path
) -> None:
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    async with session_factory() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(seller),
            CreateDealInput(
                title="Laptop",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
        deal_id = deal.id

    fake = FakeTelegramGateway()
    bot = FakeBot()
    await _worker_run(session_factory, fake, bot, tmp_path / "none.png")

    async with session_factory() as session:
        membership = (
            await session.execute(
                select(TelegramMembership).where(
                    TelegramMembership.deal_id == deal_id,
                    TelegramMembership.user_id == buyer,
                )
            )
        ).scalar_one()
        link = membership.invite_link
    assert link and link.startswith("https://t.me/+fake-invite-")

    to_buyer = [text for chat, text, _ in bot.sent if chat == BUYER_TG]
    assert any(link in text for text in to_buyer), to_buyer
    # The link is sent to the bot, never stored in the web notification list.
    async with session_factory() as session:
        bodies = (await session.execute(select(Notification.body))).scalars().all()
    assert bodies and all(link not in body and "t.me" not in body for body in bodies)


async def test_seller_also_gets_a_personal_link_not_a_direct_add(
    session_factory: Factory, pricing: PricingService, make_user: Any, tmp_path: Path
) -> None:
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    async with session_factory() as session:
        await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title="Laptop",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
    fake = FakeTelegramGateway()
    bot = FakeBot()
    await _worker_run(session_factory, fake, bot, tmp_path / "none.png")

    assert all(not users for users in fake.members.values())  # nobody added directly
    links_by_chat: dict[int, str] = {}
    for chat in (BUYER_TG, SELLER_TG):
        texts = [text for c, text, _ in bot.sent if c == chat and "t.me/+fake-invite-" in text]
        assert len(texts) == 1, (chat, bot.sent)
        links_by_chat[chat] = texts[0].split("\n")[-1]
    assert links_by_chat[BUYER_TG] != links_by_chat[SELLER_TG]


# ---------- Web app notification list ----------


def _create_deal_for(db_url: str, seller: int, buyer: int) -> str:
    """The buyer creates the deal, so the seller is the counterparty who gets the notification."""

    async def run() -> str:
        engine = create_engine_from_url(db_url)
        factory = create_session_factory(engine)
        pricing = PricingService(service_fee_bps=1000, max_amount_uzs=1_000_000_000)
        try:
            async with factory() as session:
                deal: Deal = await DealService(session, pricing).create_deal(
                    Actor.user(buyer),
                    CreateDealInput(
                        title="Telefon",
                        buyer_id=buyer,
                        seller_id=seller,
                        product_amount_tiyin=100_000_000,
                        terms_text="terms",
                    ),
                )
                return deal.public_id
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_notification_list_is_private_and_marks_read(api: TestClient, db_url: str) -> None:
    buyer = _login(api, BUYER_TG)
    seller = _login(api, SELLER_TG)
    public_id = _create_deal_for(db_url, seller["user_id"], buyer["user_id"])

    # The seller is the counterparty: one unread "new deal" notification.
    as_seller = api.get("/api/v1/notifications", headers=_auth(seller)).json()
    assert as_seller["unread_count"] == 1
    item = as_seller["data"][0]
    assert item["kind"] == "deal_created"
    assert item["deal_id"] == public_id
    assert item["read"] is False
    assert "Telefon" in item["body"]

    # The buyer (creator) gets nothing, and cannot see the seller's list.
    assert api.get("/api/v1/notifications", headers=_auth(buyer)).json()["data"] == []

    # Another user cannot mark it read: same 404 as a missing row.
    assert api.post(f"/api/v1/notifications/{item['id']}/read", headers=_auth(buyer)).status_code == 404

    marked = api.post(f"/api/v1/notifications/{item['id']}/read", headers=_auth(seller))
    assert marked.status_code == 200 and marked.json()["read"] is True
    assert api.get("/api/v1/notifications", headers=_auth(seller)).json()["unread_count"] == 0


def test_read_all_and_pagination(api: TestClient, db_url: str) -> None:
    buyer = _login(api, 7201)
    seller = _login(api, 7202)
    for _ in range(3):
        _create_deal_for(db_url, seller["user_id"], buyer["user_id"])

    # The buyer created these deals, so the seller is the one who has notifications to page through.
    page = api.get("/api/v1/notifications?limit=2", headers=_auth(seller)).json()
    assert len(page["data"]) == 2 and page["unread_count"] == 3
    assert page["pagination"]["has_more"] is True and page["pagination"]["next_cursor"]

    rest = api.get(
        f"/api/v1/notifications?limit=2&cursor={page['pagination']['next_cursor']}",
        headers=_auth(seller),
    ).json()
    assert len(rest["data"]) == 1 and rest["pagination"]["has_more"] is False

    done = api.post("/api/v1/notifications/read-all", headers=_auth(seller)).json()
    assert done == {"updated": 3}
    assert api.get("/api/v1/notifications", headers=_auth(seller)).json()["unread_count"] == 0
