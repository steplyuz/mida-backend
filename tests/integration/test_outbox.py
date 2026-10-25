import contextlib
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.domain_errors import InvalidAmountError
from app.core.time import utcnow
from app.models.enums import OutboxStatus
from app.models.outbox import OutboxEvent
from app.services.deal_service import CreateDealInput, DealService
from app.services.outbox_service import GROUP_EVENT, OutboxService
from app.services.pricing_service import PricingService

Factory = async_sessionmaker[AsyncSession]


def _data(buyer: int, seller: int, amount: int = 100_000_000) -> CreateDealInput:
    return CreateDealInput(
        title="Item",
        buyer_id=buyer,
        seller_id=seller,
        product_amount_tiyin=amount,
        terms_text="terms",
    )


async def _count_events(factory: Factory) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)


async def test_deal_creation_enqueues_exactly_one_group_event(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    buyer, seller = await make_user(3001), await make_user(3002)
    async with session_factory() as session:
        deal = await DealService(session, pricing).create_deal(Actor.user(buyer), _data(buyer, seller))
    async with session_factory() as session:
        event = await OutboxService(session).find_by_dedupe_key(f"{GROUP_EVENT}:{deal.id}")
    assert event is not None
    assert event.status is OutboxStatus.PENDING
    async with session_factory() as session:
        group_events = await session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == GROUP_EVENT)
        )
    assert group_events == 1


async def test_rejected_deal_leaves_no_outbox_event(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    buyer, seller = await make_user(3011), await make_user(3012)
    async with session_factory() as session:
        with contextlib.suppress(InvalidAmountError):
            await DealService(session, pricing).create_deal(Actor.user(buyer), _data(buyer, seller, amount=0))
    assert await _count_events(session_factory) == 0


async def test_enqueue_with_same_dedupe_key_returns_the_existing_event(
    session_factory: Factory,
) -> None:
    now = utcnow()
    async with session_factory() as session:
        first = await OutboxService(session).enqueue(
            event_type="x", aggregate_id=1, dedupe_key="x:1", payload={}, now=now
        )
        second = await OutboxService(session).enqueue(
            event_type="x", aggregate_id=1, dedupe_key="x:1", payload={}, now=now
        )
        await session.commit()
    assert first.id == second.id
    assert await _count_events(session_factory) == 1


async def test_only_one_of_two_concurrent_claims_wins(
    session_factory: Factory,
) -> None:
    now = utcnow()
    async with session_factory() as session:
        await OutboxService(session).enqueue(
            event_type="x", aggregate_id=1, dedupe_key="race:1", payload={"deal_id": 1}, now=now
        )
        await session.commit()

    async def claim() -> int:
        async with session_factory() as session:
            return len(await OutboxService(session).claim_due(now=now, lease_seconds=60, limit=10))

    counts = [await claim(), await claim()]
    assert sorted(counts) == [0, 1]


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_finish(
    session_factory: Factory,
) -> None:
    start = utcnow()
    async with session_factory() as session:
        await OutboxService(session).enqueue(
            event_type="x", aggregate_id=2, dedupe_key="lease:1", payload={"deal_id": 2}, now=start
        )
        await session.commit()

    async with session_factory() as session:
        first = (await OutboxService(session).claim_due(now=start, lease_seconds=60, limit=5))[0]

    later = start + timedelta(seconds=120)
    async with session_factory() as session:
        second = (await OutboxService(session).claim_due(now=later, lease_seconds=60, limit=5))[0]
    assert second.attempt_count == 2 and second.lease_token != first.lease_token

    async with session_factory() as session:
        stale_outcome = await OutboxService(session).complete(first, now=later)
    assert stale_outcome is False  # the first worker lost its lease and cannot overwrite the result

    async with session_factory() as session:
        fresh_outcome = await OutboxService(session).complete(second, now=later)
    assert fresh_outcome is True
