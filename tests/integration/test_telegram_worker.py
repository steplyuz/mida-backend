"""Group creation end to end: outbox worker and group service, migrated DB, fake gateway."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError
from app.integrations.telegram.factory import UnconfiguredGateway
from app.models.deal import Deal
from app.models.enums import MembershipKind, OutboxStatus, TelegramGroupStatus
from app.models.outbox import OutboxEvent
from app.models.telegram import TelegramMembership
from app.services.deal_service import CreateDealInput, DealService
from app.services.outbox_service import AVATAR_EVENT, GROUP_EVENT, OutboxService
from app.services.pricing_service import PricingService
from app.services.telegram_group_service import logo_is_usable
from app.workers.outbox_worker import OutboxWorker
from app.workers.retry_policy import RetryPolicy
from tests.support.fake_telegram import FakeTelegramGateway

Factory = async_sessionmaker[AsyncSession]
BUYER_TG, SELLER_TG = 4001, 4002


@pytest.fixture
def logo(tmp_path: Path) -> Path:
    path = tmp_path / "mida-logo.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


def _worker(
    factory: Factory, gateway: Any, logo_path: Path, policy: RetryPolicy | None = None
) -> OutboxWorker:
    return OutboxWorker(
        factory,
        gateway,
        logo_path=logo_path,
        invite_ttl_seconds=86_400,
        policy=policy or RetryPolicy(),
        rng=random.Random(7),
    )


async def _new_deal(
    factory: Factory, pricing: PricingService, make_user: Any, title: str = "iPhone 15"
) -> int:
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    async with factory() as session:
        deal = await DealService(session, pricing).create_deal(
            Actor.user(buyer),
            CreateDealInput(
                title=title,
                buyer_id=buyer,
                seller_id=seller,
                product_amount_tiyin=100_000_000,
                terms_text="terms",
            ),
        )
    return deal.id


async def _deal(factory: Factory, deal_id: int) -> Deal:
    async with factory() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        return deal


async def _group_event(factory: Factory, deal_id: int) -> OutboxEvent:
    async with factory() as session:
        event = await OutboxService(session).find_by_dedupe_key(f"{GROUP_EVENT}:{deal_id}")
        assert event is not None
        return event


async def _memberships(factory: Factory, deal_id: int) -> list[TelegramMembership]:
    async with factory() as session:
        rows = await session.execute(select(TelegramMembership).where(TelegramMembership.deal_id == deal_id))
        return list(rows.scalars().all())


async def test_happy_path_creates_group_adds_parties_and_sets_logo(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    worker = _worker(session_factory, fake, logo)

    # Group event plus the "deal created" notification (it fails quietly: no bot in this test).
    assert await worker.run_once() == 2

    deal = await _deal(session_factory, deal_id)
    assert deal.telegram_group_status is TelegramGroupStatus.CREATED
    assert deal.telegram_chat_title == f"{deal.deal_number} | iPhone 15"
    assert deal.telegram_chat_id is not None
    assert [title for _, title in fake.groups] == [deal.telegram_chat_title]
    assert fake.members.get(deal.telegram_chat_id, set()) == set()  # no direct adds
    assert len(fake.invite_links) == 2  # one personal link per party
    assert (await _group_event(session_factory, deal_id)).status is OutboxStatus.DONE

    # Logo is a separate, later task. Party notifications queued by the group step run first.
    for _ in range(3):
        await worker.run_once()
    assert fake.photos == [deal.telegram_chat_id]


async def test_group_is_never_created_twice_for_one_deal(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    worker = _worker(session_factory, fake, logo)
    await worker.run_once()

    async with session_factory() as session:
        again = await OutboxService(session).enqueue(
            event_type=GROUP_EVENT,
            aggregate_id=deal_id,
            dedupe_key=f"{GROUP_EVENT}:{deal_id}",
            payload={"deal_id": deal_id},
        )
        await session.commit()
    assert again.status is OutboxStatus.DONE
    await worker.run_once()
    assert len(fake.groups) == 1


async def test_rate_limit_is_retried_after_the_server_delay_and_resumes_without_recreating(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    fake.queue_failure("invite", TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=60))
    worker = _worker(session_factory, fake, logo)
    start = (await _group_event(session_factory, deal_id)).next_attempt_at

    await worker.run_once(now=start)
    event = await _group_event(session_factory, deal_id)
    assert event.status is OutboxStatus.PENDING and event.attempt_count == 1
    assert event.last_error_code == "rate_limit"
    assert event.next_attempt_at == start + timedelta(seconds=60)
    assert len(fake.groups) == 1  # the group exists; the retry must not create another

    await worker.run_once(now=start + timedelta(seconds=10))  # group task is not due; avatar may be
    group_now = await _group_event(session_factory, deal_id)
    assert group_now.status is OutboxStatus.PENDING and group_now.attempt_count == 1

    await worker.run_once(now=start + timedelta(seconds=61))
    assert len(fake.groups) == 1
    assert (await _group_event(session_factory, deal_id)).status is OutboxStatus.DONE
    assert len(await _memberships(session_factory, deal_id)) == 2


async def test_temporary_failure_uses_exponential_backoff_with_jitter(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    fake.queue_failure("create", TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK))
    worker = _worker(session_factory, fake, logo)
    start = (await _group_event(session_factory, deal_id)).next_attempt_at

    await worker.run_once(now=start)
    event = await _group_event(session_factory, deal_id)
    delay = (event.next_attempt_at - start).total_seconds()
    assert 24 <= delay <= 36  # 30s base with +-20% jitter
    assert event.last_error_code == "temporary_network"


async def test_group_creation_failure_is_dead_lettered_and_marks_the_deal(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    fake.queue_failure("create", TelegramGatewayError(ErrorCategory.GROUP_CREATION))
    await _worker(session_factory, fake, logo).run_once()

    event = await _group_event(session_factory, deal_id)
    assert event.status is OutboxStatus.FAILED
    assert event.last_error_code == "group_creation"
    assert (await _deal(session_factory, deal_id)).telegram_group_status is TelegramGroupStatus.FAILED
    assert fake.groups == []


async def test_attempt_budget_ends_in_failure(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    for _ in range(2):
        fake.queue_failure("create", TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK))
    worker = _worker(session_factory, fake, logo, policy=RetryPolicy(max_attempts=2))
    start = (await _group_event(session_factory, deal_id)).next_attempt_at

    await worker.run_once(now=start)
    await worker.run_once(now=start + timedelta(days=1))

    event = await _group_event(session_factory, deal_id)
    assert event.status is OutboxStatus.FAILED and event.attempt_count == 2
    assert (await _deal(session_factory, deal_id)).telegram_group_status is TelegramGroupStatus.FAILED


async def test_every_party_gets_their_own_one_use_invite_only_they_can_see(
    session_factory: Factory,
    pricing: PricingService,
    make_user: Any,
    logo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    caplog.set_level(logging.INFO)
    await _worker(session_factory, fake, logo).run_once()

    memberships = await _memberships(session_factory, deal_id)
    assert len(memberships) == 2
    assert {m.kind for m in memberships} == {MembershipKind.INVITE}
    links = {m.invite_link for m in memberships}
    assert len(links) == 2 and None not in links
    assert all(link is not None and link.startswith("https://t.me/+fake-invite-") for link in links)
    assert len(fake.invite_links) == 2
    assert all(not users for users in fake.members.values())  # nobody added directly
    assert all(link is None or link not in caplog.text for link in links)  # invite links never reach the logs


async def test_logo_failure_does_not_block_the_group(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    fake.queue_failure("photo", TelegramGatewayError(ErrorCategory.AVATAR))
    worker = _worker(session_factory, fake, logo)
    await worker.run_once()
    await worker.run_once(now=utcnow_plus(hours=1))

    deal = await _deal(session_factory, deal_id)
    assert deal.telegram_group_status is TelegramGroupStatus.CREATED
    assert len(fake.groups) == 1
    avatar = await _avatar_event(session_factory, deal_id)
    assert avatar.status is OutboxStatus.PENDING and avatar.last_error_code == "avatar"


async def test_missing_logo_is_skipped_not_failed(
    session_factory: Factory, pricing: PricingService, make_user: Any, tmp_path: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    worker = _worker(session_factory, fake, tmp_path / "does-not-exist.png")
    await worker.run_once()
    await worker.run_once()
    assert fake.photos == []
    assert (await _avatar_event(session_factory, deal_id)).status is OutboxStatus.DONE


async def test_unconfigured_telegram_fails_clearly_and_visibly(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    await _worker(session_factory, UnconfiguredGateway(), logo).run_once()
    event = await _group_event(session_factory, deal_id)
    assert event.status is OutboxStatus.FAILED
    assert event.last_error_code == "configuration"


async def test_crashed_worker_lease_is_recovered_by_another_worker(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    start = (await _group_event(session_factory, deal_id)).next_attempt_at
    async with session_factory() as session:
        # A worker claimed the event and died without recording an outcome.
        await OutboxService(session).claim_due(now=start, lease_seconds=60, limit=10)

    fake = FakeTelegramGateway()
    await _worker(session_factory, fake, logo).run_once(now=start + timedelta(seconds=300))
    assert len(fake.groups) == 1
    assert (await _group_event(session_factory, deal_id)).status is OutboxStatus.DONE


async def test_two_parallel_workers_do_not_duplicate_the_group(
    session_factory: Factory, pricing: PricingService, make_user: Any, logo: Path
) -> None:
    deal_id = await _new_deal(session_factory, pricing, make_user)
    fake = FakeTelegramGateway()
    workers = [_worker(session_factory, fake, logo) for _ in range(3)]
    await asyncio.gather(*(w.run_once() for w in workers))
    assert len(fake.groups) == 1
    assert (await _group_event(session_factory, deal_id)).status is OutboxStatus.DONE


async def test_unknown_event_type_is_failed_permanently(session_factory: Factory, logo: Path) -> None:
    async with session_factory() as session:
        await OutboxService(session).enqueue(
            event_type="mystery", aggregate_id=1, dedupe_key="mystery:1", payload={"deal_id": 1}
        )
        await session.commit()
    await _worker(session_factory, FakeTelegramGateway(), logo).run_once()
    async with session_factory() as session:
        event = await OutboxService(session).find_by_dedupe_key("mystery:1")
    assert event is not None and event.status is OutboxStatus.FAILED
    assert event.last_error_code == "unknown_event_type"


def test_logo_validation_rules(tmp_path: Path) -> None:
    good = tmp_path / "a.png"
    good.write_bytes(b"x")
    bad_type = tmp_path / "a.gif"
    bad_type.write_bytes(b"x")
    assert logo_is_usable(good)
    assert not logo_is_usable(bad_type)
    assert not logo_is_usable(tmp_path / "missing.png")


def utcnow_plus(**kwargs: float) -> Any:
    from app.core.time import utcnow

    return utcnow() + timedelta(**kwargs)


async def _avatar_event(factory: Factory, deal_id: int) -> OutboxEvent:
    async with factory() as session:
        event = await OutboxService(session).find_by_dedupe_key(f"{AVATAR_EVENT}:{deal_id}")
        assert event is not None
        return event
