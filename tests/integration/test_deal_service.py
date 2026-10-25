from __future__ import annotations

import asyncio
import dataclasses

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.core.domain_errors import (
    ConcurrencyConflictError,
    InvalidAmountError,
    InvalidTransitionError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import request_id_ctx
from app.models.audit import AuditLog
from app.models.deal import Deal
from app.models.enums import DealStatus as S
from app.services.deal_service import CreateDealInput, DealService
from app.services.pricing_service import PricingService

from .conftest import MakeUser

Factory = async_sessionmaker[AsyncSession]


def _input(buyer: int, seller: int, amount: int = 100_000_000, title: str = "iPhone 15") -> CreateDealInput:
    return CreateDealInput(
        title=title,
        buyer_id=buyer,
        seller_id=seller,
        product_amount_tiyin=amount,
        terms_text="Standard MIDA terms v1",
    )


async def _create(factory: Factory, pricing: PricingService, actor: Actor, data: CreateDealInput) -> Deal:
    async with factory() as session:
        return await DealService(session, pricing).create_deal(actor, data)


async def _step(factory: Factory, pricing: PricingService, actor: Actor, deal: Deal, target: S) -> Deal:
    async with factory() as session:
        return await DealService(session, pricing).transition(
            actor, deal.id, target, expected_version=deal.version
        )


async def _audit_rows(factory: Factory, deal_id: int) -> list[AuditLog]:
    async with factory() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.deal_id == deal_id).order_by(AuditLog.id)
        )
        return list(result.scalars().all())


# ---------- creation and pricing ----------


async def test_create_deal_stores_server_computed_price(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    assert deal.deal_number == "MIDA-000001"
    assert deal.product_amount_tiyin == 100_000_000
    assert deal.service_fee_bps == 1000
    assert deal.service_fee_tiyin == 10_000_000
    assert deal.total_amount_tiyin == 110_000_000
    assert deal.status is S.CREATED
    assert deal.version == 1


async def test_client_cannot_supply_fee_or_total() -> None:
    field_names = {field.name for field in dataclasses.fields(CreateDealInput)}
    assert "service_fee_tiyin" not in field_names
    assert "total_amount_tiyin" not in field_names
    assert "service_fee_bps" not in field_names


async def test_deal_numbers_increase_sequentially(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    first = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    second = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    assert (first.deal_number, second.deal_number) == ("MIDA-000001", "MIDA-000002")


async def test_rejected_amount_does_not_consume_a_deal_number(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    with pytest.raises(InvalidAmountError):
        await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller, amount=0))
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    assert deal.deal_number == "MIDA-000001"


async def test_parallel_creation_yields_unique_gapless_numbers(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    results = await asyncio.gather(
        *(
            _create(
                session_factory,
                pricing,
                Actor.user(buyer),
                _input(buyer, seller, title=f"Item {i}"),
            )
            for i in range(10)
        )
    )
    numbers = sorted(deal.deal_number for deal in results)
    assert numbers == [f"MIDA-{n:06d}" for n in range(1, 11)]


async def test_terms_snapshot_keeps_the_rate_in_force_at_creation(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    old = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    lowered = PricingService(service_fee_bps=500, max_amount_uzs=1_000_000_000)
    new = await _create(session_factory, lowered, Actor.user(buyer), _input(buyer, seller))

    assert old.service_fee_bps == 1000 and old.service_fee_tiyin == 10_000_000
    assert new.service_fee_bps == 500 and new.service_fee_tiyin == 5_000_000
    assert '"service_fee_bps": 1000' in old.terms_snapshot_json


async def test_buyer_and_seller_must_differ(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    user = await make_user(1001)
    with pytest.raises(InvariantViolationError):
        await _create(session_factory, pricing, Actor.user(user), _input(user, user))


async def test_inactive_party_cannot_be_used(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer = await make_user(1001)
    inactive_seller = await make_user(1002, active=False)
    with pytest.raises(InvariantViolationError):
        await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, inactive_seller))


async def test_user_cannot_create_a_deal_between_others(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller, outsider = await make_user(1001), await make_user(1002), await make_user(1003)
    with pytest.raises(PermissionDeniedError):
        await _create(session_factory, pricing, Actor.user(outsider), _input(buyer, seller))


# ---------- access control ----------


async def test_non_participant_cannot_see_or_change_a_deal(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller, outsider = await make_user(1001), await make_user(1002), await make_user(1003)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    async with session_factory() as session:
        service = DealService(session, pricing)
        with pytest.raises(NotFoundError):
            await service.get_deal(Actor.user(outsider), deal.id)
    with pytest.raises(NotFoundError):
        await _step(session_factory, pricing, Actor.user(outsider), deal, S.CANCELLED)


async def test_missing_deal_and_foreign_deal_look_identical(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller, outsider = await make_user(1001), await make_user(1002), await make_user(1003)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    async with session_factory() as session:
        service = DealService(session, pricing)
        with pytest.raises(NotFoundError) as foreign:
            await service.get_deal(Actor.user(outsider), deal.id)
        with pytest.raises(NotFoundError) as missing:
            await service.get_deal(Actor.user(outsider), 999_999)
    assert foreign.value.message == missing.value.message


# ---------- state transitions ----------


async def test_full_happy_path_writes_one_audit_event_per_change(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    steps: list[tuple[Actor, S]] = [
        (Actor.user(buyer), S.AWAITING_PAYMENT),
        (Actor.system(), S.PAYMENT_PENDING_VERIFICATION),
        (Actor.system(), S.PAYMENT_CONFIRMED),
        (Actor.user(seller), S.DELIVERY_IN_PROGRESS),
        (Actor.user(seller), S.AWAITING_BUYER_CONFIRMATION),
        (Actor.user(buyer), S.COMPLETED),
    ]
    for actor, target in steps:
        deal = await _step(session_factory, pricing, actor, deal, target)

    assert deal.status is S.COMPLETED
    assert deal.completed_at is not None
    assert deal.version == 7  # created=1, plus six transitions

    rows = await _audit_rows(session_factory, deal.id)
    assert [row.action for row in rows] == ["deal.created"] + ["deal.status_changed"] * 6
    first_change = rows[1]
    assert first_change.actor_user_id == buyer
    assert first_change.actor_kind == "user"
    assert '"status": "created"' in (first_change.old_values_json or "")
    assert '"status": "awaiting_payment"' in (first_change.new_values_json or "")


async def test_invalid_transition_is_rejected_and_state_is_unchanged(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    with pytest.raises(InvalidTransitionError):
        await _step(session_factory, pricing, Actor.user(buyer), deal, S.COMPLETED)

    async with session_factory() as session:
        current = await session.get(Deal, deal.id)
        assert current is not None and current.status is S.CREATED and current.version == 1
    assert len(await _audit_rows(session_factory, deal.id)) == 1  # only deal.created


async def test_wrong_role_is_rejected(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    with pytest.raises(PermissionDeniedError):
        await _step(session_factory, pricing, Actor.user(seller), deal, S.AWAITING_PAYMENT)


async def test_stale_expected_version_is_rejected(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    async with session_factory() as session:
        with pytest.raises(ConcurrencyConflictError):
            await DealService(session, pricing).transition(
                Actor.user(buyer), deal.id, S.AWAITING_PAYMENT, expected_version=99
            )


async def test_concurrent_identical_transitions_only_one_succeeds(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    outcomes: list[Deal | BaseException] = list(
        await asyncio.gather(
            _step(session_factory, pricing, Actor.user(buyer), deal, S.AWAITING_PAYMENT),
            _step(session_factory, pricing, Actor.user(buyer), deal, S.AWAITING_PAYMENT),
            return_exceptions=True,
        )
    )
    successes = [o for o in outcomes if isinstance(o, Deal)]
    conflicts = [o for o in outcomes if isinstance(o, ConcurrencyConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with session_factory() as session:
        current = await session.get(Deal, deal.id)
        assert current is not None and current.version == 2
    assert len(await _audit_rows(session_factory, deal.id)) == 2  # created + one change


async def test_admin_can_cancel_and_cancelled_at_is_set(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller, admin = await make_user(1001), await make_user(1002), await make_user(1003)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))
    cancelled = await _step(session_factory, pricing, Actor.admin(admin), deal, S.CANCELLED)
    assert cancelled.status is S.CANCELLED
    assert cancelled.cancelled_at is not None
    rows = await _audit_rows(session_factory, deal.id)
    assert rows[-1].actor_kind == "admin"


async def test_reason_and_correlation_id_are_recorded(
    session_factory: Factory, pricing: PricingService, make_user: MakeUser
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    deal = await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    token = request_id_ctx.set("req-test-123")
    try:
        async with session_factory() as session:
            await DealService(session, pricing).transition(
                Actor.user(buyer),
                deal.id,
                S.CANCELLED,
                expected_version=deal.version,
                reason="changed my mind",
            )
    finally:
        request_id_ctx.reset(token)

    last = (await _audit_rows(session_factory, deal.id))[-1]
    assert last.correlation_id == "req-test-123"
    assert "changed my mind" in (last.new_values_json or "")


# ---------- audit immutability ----------


async def test_audit_rows_cannot_be_updated_or_deleted(
    engine: AsyncEngine,
    session_factory: Factory,
    pricing: PricingService,
    make_user: MakeUser,
) -> None:
    buyer, seller = await make_user(1001), await make_user(1002)
    await _create(session_factory, pricing, Actor.user(buyer), _input(buyer, seller))

    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("UPDATE audit_logs SET action = 'tampered'"))
        await conn.rollback()
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("DELETE FROM audit_logs"))
        await conn.rollback()
