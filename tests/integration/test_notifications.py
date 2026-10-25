"""Bot notifications: enqueue inside the business transaction, send through the worker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError
from app.models.enums import DealStatus, OutboxStatus
from app.models.outbox import OutboxEvent
from app.services.deal_service import CreateDealInput, DealService
from app.services.outbox_service import NOTIFY_EVENT
from app.services.pricing_service import PricingService
from app.workers.outbox_worker import OutboxWorker
from tests.support.fake_bot import FakeBot
from tests.support.fake_telegram import FakeTelegramGateway

Factory = async_sessionmaker[AsyncSession]
BUYER_TG, SELLER_TG = 6001, 6002


def _worker(sf: Factory, bot: FakeBot, **kw: Any) -> OutboxWorker:
    return OutboxWorker(
        sf,
        FakeTelegramGateway(),
        logo_path=Path("missing.png"),
        invite_ttl_seconds=60,
        bot=bot,
        **kw,
    )


async def _new_deal(sf: Factory, pricing: PricingService, make_user: Any) -> tuple[int, int, int]:
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    async with sf() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title="iPhone 15",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
        return deal.id, buyer, seller


async def _notify_events(sf: Factory) -> list[OutboxEvent]:
    async with sf() as session:
        rows = await session.execute(select(OutboxEvent).where(OutboxEvent.event_type == NOTIFY_EVENT))
        return list(rows.scalars())


async def test_counterparty_gets_a_bot_message_when_a_deal_is_created(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    await _new_deal(session_factory, pricing, make_user)
    bot = FakeBot()
    await _worker(session_factory, bot, miniapp_url="https://app.example").run_once()

    # The creator (buyer) is not notified about their own action; the seller is.
    assert [chat for chat, _, _ in bot.sent] == [SELLER_TG]
    chat, text, url = bot.sent[0]
    assert "iPhone 15" in text and "sotuvchi" in text
    assert url is not None and url.startswith("https://app.example/deals/")


async def test_status_change_notifies_the_other_party_only(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id, buyer, seller = await _new_deal(session_factory, pricing, make_user)
    bot = FakeBot()
    worker = _worker(session_factory, bot)
    await worker.run_once()
    bot.sent.clear()

    async with session_factory() as session:
        service = DealService(session, pricing)
        deal = await service.get_deal(Actor.user(seller), deal_id)
        await service.transition(
            Actor.user(seller), deal_id, DealStatus.CANCELLED, expected_version=deal.version
        )
    await worker.run_once()

    # Group-join messages may also be delivered in this run; only the status change matters here.
    status_msgs = [(chat, text) for chat, text, _ in bot.sent if "Bitim holati" in text]
    assert [chat for chat, _ in status_msgs] == [BUYER_TG]
    assert "bekor qilindi" in status_msgs[0][1]


async def test_unreachable_user_is_skipped_not_dead_lettered(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    await _new_deal(session_factory, pricing, make_user)
    bot = FakeBot(failures={SELLER_TG: TelegramGatewayError(ErrorCategory.USER_UNREACHABLE)})
    worker = _worker(session_factory, bot)
    for _ in range(3):  # group, then the notifications it queues
        await worker.run_once()

    events = await _notify_events(session_factory)
    assert events and all(e.status is OutboxStatus.DONE for e in events)
    assert [chat for chat, _, _ in bot.sent if chat == SELLER_TG] == []


async def test_network_error_is_retried_later(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    await _new_deal(session_factory, pricing, make_user)
    bot = FakeBot(failures={SELLER_TG: TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK)})
    await _worker(session_factory, bot).run_once()

    events = [e for e in await _notify_events(session_factory) if '"deal_created"' in e.payload_json]
    assert [e.status for e in events] == [OutboxStatus.PENDING]
    assert events[0].last_error_code == "temporary_network"
