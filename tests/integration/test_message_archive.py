"""Message archive: ingestion, dedup, edit history, deletions, and atomic failure handling."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import Actor
from app.integrations.telegram.updates import ChatMessageEvent, parse_update
from app.models.deal import Deal
from app.models.message import DealMessage, MessageRevision, TelegramUpdateReceipt
from app.services.deal_service import CreateDealInput, DealService
from app.services.message_service import MessageService
from app.services.pricing_service import PricingService

Factory = async_sessionmaker[AsyncSession]
CHAT_A, CHAT_B = -1001000000111, -1001000000222
BUYER_TG, SELLER_TG, STRANGER_TG = 6001, 6002, 6099
WHEN = 1_780_000_000


_PARTY_IDS: dict[str, int] = {}


async def _parties(make_user: Any, reuse: bool) -> tuple[int, int]:
    """Users are created once per test; a second deal in the same test reuses them."""
    if reuse and _PARTY_IDS:
        return _PARTY_IDS["buyer"], _PARTY_IDS["seller"]
    buyer, seller = await make_user(BUYER_TG), await make_user(SELLER_TG)
    _PARTY_IDS.update(buyer=buyer, seller=seller)
    return buyer, seller


async def _deal_in_chat(
    factory: Factory,
    pricing: PricingService,
    make_user: Any,
    chat_id: int,
    title: str,
    *,
    reuse: bool = False,
) -> int:
    buyer, seller = await _parties(make_user, reuse)
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
        deal.telegram_chat_id = chat_id
        await session.commit()
        return deal.id


def _event(
    update_id: int,
    message_id: int,
    text: str,
    *,
    chat: int = CHAT_A,
    sender: int = BUYER_TG,
    kind: str = "message",
) -> tuple[int, ChatMessageEvent]:
    key = "edited_message" if kind == "edited" else "message"
    message = {
        "message_id": message_id,
        "date": WHEN,
        "chat": {"id": chat, "type": "supergroup"},
        "from": {"id": sender, "is_bot": False, "first_name": "Ali"},
        "text": text,
    }
    if kind == "edited":
        message["edit_date"] = WHEN + 30
    parsed = parse_update({"update_id": update_id, key: message})
    assert parsed is not None
    return parsed


async def _messages(factory: Factory, deal_id: int) -> list[DealMessage]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(DealMessage).where(DealMessage.deal_id == deal_id).order_by(DealMessage.id)
                )
            )
            .scalars()
            .all()
        )


async def test_message_is_stored_once_and_attributed_to_the_participant(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    update_id, event = _event(1001, 10, "Salom, buyum bormi?")
    async with session_factory() as session:
        outcome = await MessageService(session).ingest(update_id, event)
    assert outcome == "stored"
    [row] = await _messages(session_factory, deal_id)
    assert row.text_content == "Salom, buyum bormi?"
    assert row.sender_user_id is not None
    assert row.sender_telegram_id == BUYER_TG


async def test_redelivered_update_is_recognised_and_skipped(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    update_id, event = _event(1002, 11, "once")
    async with session_factory() as session:
        first = await MessageService(session).ingest(update_id, event)
    async with session_factory() as session:
        second = await MessageService(session).ingest(update_id, event)
    assert (first, second) == ("stored", "duplicate")
    assert len(await _messages(session_factory, deal_id)) == 1


async def test_same_message_under_a_new_update_id_is_still_one_row(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        await MessageService(session).ingest(*_event(1003, 12, "same"))
    async with session_factory() as session:
        outcome = await MessageService(session).ingest(*_event(1004, 12, "same"))
    assert outcome == "duplicate"
    assert len(await _messages(session_factory, deal_id)) == 1


async def test_edits_keep_every_previous_text_as_a_revision(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        await MessageService(session).ingest(*_event(1010, 20, "narx 100"))
    async with session_factory() as session:
        assert await MessageService(session).ingest(*_event(1011, 20, "narx 120", kind="edited")) == "edited"
    async with session_factory() as session:
        assert (
            await MessageService(session).ingest(*_event(1012, 20, "narx 120", kind="edited")) == "unchanged"
        )
    async with session_factory() as session:
        assert await MessageService(session).ingest(*_event(1013, 20, "narx 150", kind="edited")) == "edited"

    [row] = await _messages(session_factory, deal_id)
    assert row.text_content == "narx 150"
    assert row.revision_count == 2
    async with session_factory() as session:
        revisions = list(
            (
                await session.execute(
                    select(MessageRevision)
                    .where(MessageRevision.message_id == row.id)
                    .order_by(MessageRevision.revision_no)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.revision_no, r.previous_text) for r in revisions] == [
        (1, "narx 100"),
        (2, "narx 120"),
    ]


async def test_deletion_keeps_the_text_and_records_it(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        await MessageService(session).ingest(*_event(1020, 30, "bu o'chadi"))
    async with session_factory() as session:
        assert await MessageService(session).apply_deletion(CHAT_A, 30) == "deleted"
    async with session_factory() as session:
        assert await MessageService(session).apply_deletion(CHAT_A, 30) == "unchanged"

    [row] = await _messages(session_factory, deal_id)
    assert row.is_deleted and row.deleted_at is not None
    assert row.text_content == "bu o'chadi"  # the archive never silently drops content
    async with session_factory() as session:
        kinds = [
            r.change_kind.value
            for r in (
                await session.execute(select(MessageRevision).where(MessageRevision.message_id == row.id))
            ).scalars()
        ]
    assert kinds == ["deleted"]


async def test_edit_seen_before_original_is_stored_as_an_edited_message(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        outcome = await MessageService(session).ingest(*_event(1030, 40, "late view", kind="edited"))
    assert outcome == "stored"
    [row] = await _messages(session_factory, deal_id)
    assert row.edited_at is not None and row.revision_count == 0


async def test_unknown_chat_is_never_attached_to_a_deal(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        outcome = await MessageService(session).ingest(*_event(1040, 50, "wrong group", chat=-100999))
    assert outcome == "ignored_unknown_chat"
    assert await _messages(session_factory, deal_id) == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DealMessage)) == 0


async def test_messages_stay_inside_their_own_deal(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    first = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "First")
    second = await _deal_in_chat(session_factory, pricing, make_user, CHAT_B, "Second", reuse=True)
    async with session_factory() as session:
        await MessageService(session).ingest(*_event(1050, 60, "only for first", chat=CHAT_A))
    assert [m.text_content for m in await _messages(session_factory, first)] == ["only for first"]
    assert await _messages(session_factory, second) == []


async def test_failure_mid_processing_rolls_back_and_redelivery_succeeds(
    session_factory: Factory,
    pricing: PricingService,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    update_id, event = _event(1060, 70, "retry me")

    async def boom(self: MessageService, *args: Any) -> str:
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(MessageService, "_apply", boom)
    async with session_factory() as session:
        with pytest.raises(RuntimeError):
            await MessageService(session).ingest(update_id, event)

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 0
    assert await _messages(session_factory, deal_id) == []

    monkeypatch.undo()
    async with session_factory() as session:
        assert await MessageService(session).ingest(update_id, event) == "stored"
    assert len(await _messages(session_factory, deal_id)) == 1


async def test_sender_who_is_not_a_party_is_archived_without_attribution(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    await make_user(STRANGER_TG)
    async with session_factory() as session:
        await MessageService(session).ingest(*_event(1070, 80, "from a stranger", sender=STRANGER_TG))
    [row] = await _messages(session_factory, deal_id)
    assert row.sender_user_id is None and row.sender_telegram_id == STRANGER_TG


async def test_deal_lookup_uses_the_chat_mapping_not_guesswork(
    session_factory: Factory, pricing: PricingService, make_user: Any
) -> None:
    deal_id = await _deal_in_chat(session_factory, pricing, make_user, CHAT_A, "Phone")
    async with session_factory() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None and deal.telegram_chat_id == CHAT_A
