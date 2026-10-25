"""Deal endpoints. Access control lives in DealService; these handlers only translate HTTP."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request, Response

from app.api.deps import (
    CurrentUserDep,
    DealServiceDep,
    IdempotencyKeyHeader,
    SessionDep,
    enforce_rate_limit,
)
from app.core.config import Settings
from app.core.domain_errors import NotFoundError, ValidationDomainError
from app.core.pagination import decode_cursor, encode_cursor
from app.models.deal import Deal
from app.models.enums import DealStatus
from app.repositories.deal_repository import DealRepository
from app.repositories.user_repository import UserRepository
from app.schemas.deal import (
    DealCreateRequest,
    DealEventPublic,
    DealPage,
    DealPublic,
    DealUpdateRequest,
    EventPage,
    PageInfo,
    ParticipantPage,
    ParticipantPublic,
    TransitionRequest,
)
from app.services.deal_service import CreateDealInput
from app.services.idempotency_service import (
    IdempotencyClaim,
    build_request_hash,
    run_idempotent,
    validate_key,
)

router = APIRouter(prefix="/deals", tags=["deals"])

OP_CREATE = "deal.create"
OP_CANCEL = "deal.cancel"
OP_CONFIRM_RECEIPT = "deal.confirm_receipt"
REPLAY_HEADER = "Idempotent-Replayed"


async def _load_deal(session: SessionDep, deal_id: int) -> Deal:
    deal = await DealRepository(session).get_by_id(deal_id)
    if deal is None:
        raise NotFoundError("Deal not found.")
    return deal


def _parse_deal_cursor(token: str, sort: str) -> tuple[datetime, int]:
    payload = decode_cursor(token)
    try:
        if payload.get("s") != sort:
            raise ValueError("sort mismatch")
        created_at = datetime.fromisoformat(str(payload["t"]))
        if created_at.tzinfo is None:
            raise ValueError("naive timestamp")
        return created_at, int(payload["i"])
    except (KeyError, TypeError, ValueError):
        raise ValidationDomainError("Invalid pagination cursor.") from None


def _parse_id_cursor(token: str) -> int:
    payload = decode_cursor(token)
    try:
        value = int(payload["i"])
    except (KeyError, TypeError, ValueError):
        raise ValidationDomainError("Invalid pagination cursor.") from None
    if value < 1:
        raise ValidationDomainError("Invalid pagination cursor.")
    return value


@router.post("", status_code=201, response_model=DealPublic, summary="Create a deal")
async def create_deal(
    body: DealCreateRequest,
    request: Request,
    response: Response,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> DealPublic:
    key = validate_key(idempotency_key)
    # Also slows counterparty probing: each lookup spends from this per-user budget.
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        f"deal-create:{current.user.id}",
        limit=settings.rate_limit_deal_create_per_hour,
        window_seconds=3600,
    )
    counterparty = await UserRepository(session).get_by_telegram_user_id(body.counterparty_telegram_user_id)
    if counterparty is None:
        raise ValidationDomainError("The counterparty must sign in to MIDA before a deal can be created.")
    if body.my_role == "buyer":
        buyer_id, seller_id = current.user.id, counterparty.id
    else:
        buyer_id, seller_id = counterparty.id, current.user.id

    data = CreateDealInput(
        title=body.title,
        buyer_id=buyer_id,
        seller_id=seller_id,
        product_amount_tiyin=body.product_amount_tiyin,
        terms_text=body.terms_text,
        description=body.description,
    )

    async def action(claim: IdempotencyClaim) -> Deal:
        return await service.create_deal(current.actor, data, idempotency=claim)

    deal, replayed = await run_idempotent(
        session,
        user_id=current.user.id,
        operation=OP_CREATE,
        key=key,
        request_hash=build_request_hash(OP_CREATE, body.model_dump(mode="json")),
        load=lambda deal_id: _load_deal(session, deal_id),
        action=action,
    )
    if replayed:
        response.headers[REPLAY_HEADER] = "true"
    return DealPublic.build(deal, body.my_role)


@router.get("", response_model=DealPage, summary="List deals visible to the caller")
async def list_deals(
    current: CurrentUserDep,
    service: DealServiceDep,
    status: DealStatus | None = None,
    sort: Literal["created_at", "-created_at"] = "-created_at",
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
) -> DealPage:
    after = _parse_deal_cursor(cursor, sort) if cursor else None
    rows, has_more = await service.list_deals(
        current.actor,
        status=status,
        descending=sort.startswith("-"),
        after=after,
        limit=limit,
    )
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor({"s": sort, "t": last.created_at.isoformat(), "i": last.id})
    return DealPage(
        data=[DealPublic.build(deal, role) for deal, role in rows],
        pagination=PageInfo(limit=limit, next_cursor=next_cursor, has_more=has_more),
    )


@router.get("/{public_id}", response_model=DealPublic, summary="Get one deal")
async def get_deal(public_id: str, current: CurrentUserDep, service: DealServiceDep) -> DealPublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    return DealPublic.build(deal, await service.viewer_role(current.actor, deal))


@router.patch("/{public_id}", response_model=DealPublic, summary="Edit title or description")
async def update_deal(
    public_id: str,
    body: DealUpdateRequest,
    current: CurrentUserDep,
    service: DealServiceDep,
) -> DealPublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    changes: dict[str, str | None] = {
        name: getattr(body, name) for name in body.model_fields_set if name != "expected_version"
    }
    updated = await service.update_details(
        current.actor, deal.id, expected_version=body.expected_version, changes=changes
    )
    return DealPublic.build(updated, await service.viewer_role(current.actor, updated))


@router.post("/{public_id}/cancel", response_model=DealPublic, summary="Cancel a deal")
async def cancel_deal(
    public_id: str,
    body: TransitionRequest,
    response: Response,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> DealPublic:
    key = validate_key(idempotency_key)
    deal = await service.get_deal_by_public_id(current.actor, public_id)

    async def action(claim: IdempotencyClaim) -> Deal:
        return await service.transition(
            current.actor,
            deal.id,
            DealStatus.CANCELLED,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency=claim,
        )

    result, replayed = await run_idempotent(
        session,
        user_id=current.user.id,
        operation=OP_CANCEL,
        key=key,
        request_hash=build_request_hash(OP_CANCEL, {"deal": public_id, **body.model_dump(mode="json")}),
        load=lambda deal_id: _load_deal(session, deal_id),
        action=action,
    )
    if replayed:
        response.headers[REPLAY_HEADER] = "true"
    return DealPublic.build(result, await service.viewer_role(current.actor, result))


@router.post(
    "/{public_id}/confirm-receipt",
    response_model=DealPublic,
    summary="Buyer confirms receipt; completes the deal",
)
async def confirm_receipt(
    public_id: str,
    body: TransitionRequest,
    response: Response,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> DealPublic:
    key = validate_key(idempotency_key)
    deal = await service.get_deal_by_public_id(current.actor, public_id)

    async def action(claim: IdempotencyClaim) -> Deal:
        return await service.transition(
            current.actor,
            deal.id,
            DealStatus.COMPLETED,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency=claim,
        )

    result, replayed = await run_idempotent(
        session,
        user_id=current.user.id,
        operation=OP_CONFIRM_RECEIPT,
        key=key,
        request_hash=build_request_hash(
            OP_CONFIRM_RECEIPT, {"deal": public_id, **body.model_dump(mode="json")}
        ),
        load=lambda deal_id: _load_deal(session, deal_id),
        action=action,
    )
    if replayed:
        response.headers[REPLAY_HEADER] = "true"
    return DealPublic.build(result, await service.viewer_role(current.actor, result))


@router.get("/{public_id}/events", response_model=EventPage, summary="Audit trail of a deal")
async def list_events(
    public_id: str,
    current: CurrentUserDep,
    service: DealServiceDep,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=512),
) -> EventPage:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    after_id = _parse_id_cursor(cursor) if cursor else None
    rows, has_more = await service.list_events(current.actor, deal.id, after_id=after_id, limit=limit)
    next_cursor = encode_cursor({"i": rows[-1].id}) if has_more and rows else None
    return EventPage(
        data=[DealEventPublic.build(row) for row in rows],
        pagination=PageInfo(limit=limit, next_cursor=next_cursor, has_more=has_more),
    )


@router.get(
    "/{public_id}/participants",
    response_model=ParticipantPage,
    summary="Deal parties (buyer and seller)",
)
async def list_participants(
    public_id: str, current: CurrentUserDep, service: DealServiceDep
) -> ParticipantPage:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    rows = await service.list_participants(current.actor, deal.id)
    data = [
        ParticipantPublic(
            user_id=user.id,
            first_name=user.first_name,
            role=participant.participant_role,
            joined_at=participant.joined_at,
        )
        for participant, user in rows
    ]
    # A deal has exactly two parties, so the list is always complete on one page.
    return ParticipantPage(data=data, pagination=PageInfo(limit=len(data), next_cursor=None, has_more=False))
