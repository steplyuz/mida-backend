"""Idempotency under real races: concurrent requests sharing one key, on a migrated SQLite file."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.domain_errors import ConcurrencyConflictError, IdempotencyDuplicateError
from app.models.deal import Deal
from app.models.enums import DealStatus
from app.models.idempotency import IdempotencyRecord
from app.services.deal_service import CreateDealInput, DealService
from app.services.idempotency_service import IdempotencyClaim, run_idempotent
from app.services.pricing_service import PricingService

Factory = async_sessionmaker[AsyncSession]
KEY = "race-key-000001"


def _data(buyer: int, seller: int) -> CreateDealInput:
    return CreateDealInput(
        title="Race item",
        buyer_id=buyer,
        seller_id=seller,
        product_amount_tiyin=100_000_000,
        terms_text="terms",
    )


async def _count(factory: Factory, model: Any) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_five_concurrent_creates_with_one_key_yield_one_deal(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    buyer, seller = await make_user(2001), await make_user(2002)
    data = _data(buyer, seller)

    async def call() -> tuple[int, bool]:
        async with session_factory() as session:
            service = DealService(session, pricing)

            async def load(deal_id: int) -> Deal:
                deal = await session.get(Deal, deal_id)
                assert deal is not None
                return deal

            async def action(claim: IdempotencyClaim) -> Deal:
                return await service.create_deal(Actor.user(buyer), data, idempotency=claim)

            deal, replayed = await run_idempotent(
                session,
                user_id=buyer,
                operation="deal.create",
                key=KEY,
                request_hash="hash-a",
                load=load,
                action=action,
            )
            return deal.id, replayed

    results = await asyncio.gather(*(call() for _ in range(5)))

    assert len({deal_id for deal_id, _ in results}) == 1, "all callers must see the same deal"
    assert sum(1 for _, replayed in results if not replayed) == 1, "exactly one real execution"
    assert await _count(session_factory, Deal) == 1
    assert await _count(session_factory, IdempotencyRecord) == 1

    # The losers' counter increments were rolled back, so no deal number was burned.
    async with session_factory() as session:
        follow_up = await DealService(session, pricing).create_deal(Actor.user(buyer), _data(buyer, seller))
    assert follow_up.deal_number == "MIDA-000002"


async def test_concurrent_cancels_with_one_key_apply_once(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    buyer, seller = await make_user(2101), await make_user(2102)
    async with session_factory() as session:
        deal = await DealService(session, pricing).create_deal(Actor.user(buyer), _data(buyer, seller))
    deal_id = deal.id

    claim = IdempotencyClaim(user_id=buyer, operation="deal.cancel", key=KEY, request_hash="h")

    async def attempt() -> str:
        async with session_factory() as session:
            try:
                await DealService(session, pricing).transition(
                    Actor.user(buyer),
                    deal_id,
                    DealStatus.CANCELLED,
                    expected_version=1,
                    idempotency=claim,
                )
                return "applied"
            except (IdempotencyDuplicateError, ConcurrencyConflictError) as exc:
                return type(exc).__name__

    outcomes = await asyncio.gather(*(attempt() for _ in range(4)))

    assert outcomes.count("applied") == 1
    assert set(outcomes) - {"applied"} <= {"IdempotencyDuplicateError", "ConcurrencyConflictError"}
    async with session_factory() as session:
        current = await session.get(Deal, deal_id)
        assert current is not None
        assert current.status is DealStatus.CANCELLED and current.version == 2
    assert await _count(session_factory, IdempotencyRecord) == 1
