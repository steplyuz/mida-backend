"""Two parties opening a dispute at the same moment must produce exactly one dispute."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.domain_errors import ConcurrencyConflictError, InvalidTransitionError
from app.models.deal import Deal
from app.models.dispute import Dispute
from app.models.enums import DealStatus, DisputeReason
from app.services.deal_service import CreateDealInput, DealService
from app.services.dispute_service import DisputeService
from app.services.pricing_service import PricingService

Factory = async_sessionmaker[AsyncSession]


async def _confirmed_deal(factory: Factory, pricing: PricingService, make_user: Any) -> tuple[int, int, int]:
    buyer, seller = await make_user(9301), await make_user(9302)
    async with factory() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title="Lamp",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
    system = Actor.system()
    for target in (
        DealStatus.AWAITING_PAYMENT,
        DealStatus.PAYMENT_PENDING_VERIFICATION,
        DealStatus.PAYMENT_CONFIRMED,
    ):
        async with factory() as session:
            current = await session.get(Deal, deal.id)
            assert current is not None
            await DealService(session, pricing).transition(
                system, deal.id, target, expected_version=current.version
            )
    return deal.id, buyer, seller


async def test_parallel_dispute_openings_yield_exactly_one_dispute(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id, buyer, seller = await _confirmed_deal(session_factory, pricing, make_user)

    async def attempt(user: int, role_is_buyer: bool) -> str:
        async with session_factory() as session:
            current = await session.get(Deal, deal_id)
            assert current is not None
            try:
                await DisputeService(session).open(
                    Actor.user(user),
                    deal_id,
                    reason=DisputeReason.NOT_DELIVERED,
                    description="Parallel opening test.",
                    expected_version=current.version,
                )
                return "opened"
            except (InvalidTransitionError, ConcurrencyConflictError):
                return "refused"

    outcomes = await asyncio.gather(attempt(buyer, True), attempt(seller, False), attempt(buyer, True))

    assert outcomes.count("opened") == 1
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Dispute).where(Dispute.deal_id == deal_id)
        )
        deal = await session.get(Deal, deal_id)
    assert count == 1
    assert deal is not None and deal.status is DealStatus.DISPUTED
