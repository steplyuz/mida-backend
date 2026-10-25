"""Group bot: rules on join, party-only commands, private links, status posts, webhook secret."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.config import Settings
from app.core.time import utcnow
from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError
from app.main import create_app
from app.models.deal import Deal
from app.models.enums import DealStatus, MembershipKind
from app.models.telegram import TelegramMembership
from app.services.deal_service import CreateDealInput, DealService
from app.services.group_bot_service import GroupBotService
from app.services.pricing_service import PricingService
from app.workers.outbox_worker import OutboxWorker
from tests.support.fake_bot import FakeBot
from tests.support.fake_telegram import FakeTelegramGateway
from tests.support.telegram import TEST_BOT_TOKEN

Factory = async_sessionmaker[AsyncSession]
BUYER_TG, SELLER_TG, OUTSIDER_TG = 5101, 5102, 5199
CHAT = -1009000000001
TERMS = "Telefon yangi bo'lishi va kafolati bo'lishi shart."
WEBHOOK_SECRET = "w" * 32


async def _deal_in_group(sf: Factory, pricing: PricingService, make_user: Any) -> tuple[int, int, int]:
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    async with sf() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title="Telefon",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text=TERMS,
            ),
        )
        row = await session.get(Deal, deal.id)
        assert row is not None
        row.telegram_chat_id = CHAT
        await session.commit()
        return deal.id, buyer, seller


async def _worker_drain(sf: Factory, group_bot: FakeBot, tmp_path: Path) -> None:
    worker = OutboxWorker(
        sf,
        FakeTelegramGateway(),
        logo_path=tmp_path / "none.png",
        invite_ttl_seconds=60,
        bot=FakeBot(),
        group_bot=group_bot,
    )
    for _ in range(3):
        await worker.run_once()


def _group_texts(bot: FakeBot) -> list[str]:
    return [text for chat, text, _ in bot.sent if chat == CHAT]


def _update_message(
    text: str, from_tg: int, chat_id: int = CHAT, chat_type: str = "supergroup"
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": 1,
            "from": {"id": from_tg, "is_bot": False, "first_name": "X"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        }
    }


async def test_bot_joining_the_group_posts_deal_terms_once(
    session_factory: Factory, pricing: PricingService, make_user: Any, tmp_path: Path
) -> None:
    await _deal_in_group(session_factory, pricing, make_user)
    service = GroupBotService(session_factory, FakeBot(), username="MidaDealBot")
    join = {
        "my_chat_member": {
            "chat": {"id": CHAT, "type": "supergroup"},
            "new_chat_member": {"status": "administrator", "user": {"id": 1, "is_bot": True}},
        }
    }
    assert await service.handle_update(join) == "rules_queued"
    # The same join again must not post the rules a second time.
    await service.handle_update(join)

    group = FakeBot()
    await _worker_drain(session_factory, group, tmp_path)
    rules = [t for t in _group_texts(group) if "bitimi qoidalari" in t]
    assert len(rules) == 1
    assert TERMS in rules[0]


async def test_only_the_two_parties_get_answers(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    await _deal_in_group(session_factory, pricing, make_user)
    await make_user(OUTSIDER_TG)
    bot = FakeBot()
    service = GroupBotService(session_factory, bot, username="MidaDealBot")

    assert await service.handle_update(_update_message("/holat", OUTSIDER_TG)) == "ignored"
    assert _group_texts(bot) == []

    assert await service.handle_update(_update_message("/holat", BUYER_TG)) == "replied"
    assert "Bitim holati: yaratildi" in _group_texts(bot)[-1]


async def test_commands_addressed_to_another_bot_or_plain_text_are_ignored(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    await _deal_in_group(session_factory, pricing, make_user)
    bot = FakeBot()
    service = GroupBotService(session_factory, bot, username="MidaDealBot")
    assert await service.handle_update(_update_message("/holat@OtherBot", BUYER_TG)) == "ignored"
    assert await service.handle_update(_update_message("salom", BUYER_TG)) == "ignored"
    assert _group_texts(bot) == []
    assert await service.handle_update(_update_message("/holat@MidaDealBot", BUYER_TG)) == "replied"


async def test_havola_goes_only_to_the_private_chat_never_to_the_group(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id, buyer, _ = await _deal_in_group(session_factory, pricing, make_user)
    link = "https://t.me/+fake-invite-42"
    async with session_factory() as session:
        session.add(
            TelegramMembership(
                deal_id=deal_id,
                user_id=buyer,
                kind=MembershipKind.INVITE,
                invite_link=link,
                created_at=utcnow(),
            )
        )
        await session.commit()

    bot = FakeBot()
    service = GroupBotService(session_factory, bot, username="MidaDealBot")
    assert await service.handle_update(_update_message("/havola", BUYER_TG)) == "replied"

    private = [text for chat, text, _ in bot.sent if chat == BUYER_TG]
    assert private and link in private[0]
    assert all(link not in text for text in _group_texts(bot))


async def test_havola_without_private_start_tells_the_party_what_to_do(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id, buyer, _ = await _deal_in_group(session_factory, pricing, make_user)
    async with session_factory() as session:
        session.add(
            TelegramMembership(
                deal_id=deal_id,
                user_id=buyer,
                kind=MembershipKind.INVITE,
                invite_link="https://t.me/+fake-invite-7",
                created_at=utcnow(),
            )
        )
        await session.commit()
    bot = FakeBot(failures={BUYER_TG: TelegramGatewayError(ErrorCategory.USER_UNREACHABLE)})
    service = GroupBotService(session_factory, bot, username="MidaDealBot")
    await service.handle_update(_update_message("/havola", BUYER_TG))
    assert any("/start" in text for text in _group_texts(bot))
    assert all("t.me" not in text for text in _group_texts(bot))


async def test_status_change_is_posted_to_the_group(
    session_factory: Factory, pricing: PricingService, make_user: Any, tmp_path: Path
) -> None:
    deal_id, buyer, seller = await _deal_in_group(session_factory, pricing, make_user)
    async with session_factory() as session:
        service = DealService(session, pricing)
        deal = await service.get_deal(Actor.user(seller), deal_id)
        await service.transition(
            Actor.user(seller), deal_id, DealStatus.CANCELLED, expected_version=deal.version
        )

    group = FakeBot()
    await _worker_drain(session_factory, group, tmp_path)
    assert any("Bitim holati: bekor qilindi" in t for t in _group_texts(group))


def test_webhook_rejects_a_wrong_secret(db_url: str) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        group_bot_webhook_secret=SecretStr(WEBHOOK_SECRET),
        group_bot_username="MidaDealBot",
    )
    app = create_app(settings)
    app.state.group_bot = FakeBot()
    with TestClient(app) as client:
        wrong = client.post(
            "/api/v1/telegram/group-bot/webhook",
            json=_update_message("/yordam", BUYER_TG),
            headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        )
        missing = client.post("/api/v1/telegram/group-bot/webhook", json={})
        assert wrong.status_code == 401
        assert missing.status_code == 401


def test_webhook_fails_closed_when_the_secret_is_not_configured(db_url: str) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
    )
    app = create_app(settings)
    app.state.group_bot = FakeBot()
    with TestClient(app) as client:
        response = client.post("/api/v1/telegram/group-bot/webhook", json={})
    assert response.status_code == 503


def test_group_bot_webhook_is_separate_from_notification_webhook(db_url: str) -> None:
    """The group bot secret does not open the notification bot's webhook, and vice versa."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        telegram_webhook_secret=SecretStr("n" * 32),
        group_bot_webhook_secret=SecretStr(WEBHOOK_SECRET),
    )
    app = create_app(settings)
    app.state.group_bot = FakeBot()
    with TestClient(app) as client:
        other = client.post(
            "/api/v1/telegram/webhook",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        )
    assert other.status_code == 401
