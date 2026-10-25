"""Parallel payment starts with one key must produce one payment row and one transaction."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.integrations.payments.mock_provider import MockPaymentProvider
from app.models.payment import Payment
from app.services.deal_service import CreateDealInput, DealService
from app.services.payment_service import PaymentService
from app.services.pricing_service import PricingService
from tests.support.payments import MOCK_SECRET

Factory = async_sessionmaker[AsyncSession]


async def test_parallel_starts_with_one_key_produce_one_payment(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    buyer, seller = await make_user(9101), await make_user(9102)
    async with session_factory() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title="Chair",
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
    provider = MockPaymentProvider(webhook_secret=MOCK_SECRET, app_env="test")
    key = "same-key-for-every-caller"

    async def attempt() -> int:
        async with session_factory() as session:
            payment, _ = await PaymentService(session, provider).start_payment(
                Actor.user(buyer), deal.id, key
            )
            return payment.id

    ids = await asyncio.gather(*(attempt() for _ in range(4)))

    assert len(set(ids)) == 1
    async with session_factory() as session:
        rows = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.deal_id == deal.id)
        )
        transactions = (
            (await session.execute(select(Payment.provider_transaction_id).where(Payment.deal_id == deal.id)))
            .scalars()
            .all()
        )
    assert rows == 1
    assert len(set(transactions)) == 1 and transactions[0] is not None
