"""Archive reads for deal parties and administrators. Access is decided by DealService."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, DealServiceDep, SessionDep
from app.core.domain_errors import NotFoundError, ValidationDomainError
from app.core.pagination import decode_cursor, encode_cursor
from app.schemas.message import MessagePage, MessagePublic, RevisionPage, RevisionPublic
from app.services.message_service import MessageService

router = APIRouter(prefix="/deals", tags=["messages"])


def _parse_after(cursor: str) -> int:
    payload = decode_cursor(cursor)
    try:
        value = int(payload["i"])
    except (KeyError, TypeError, ValueError):
        raise ValidationDomainError("Invalid pagination cursor.") from None
    if value < 1:
        raise ValidationDomainError("Invalid pagination cursor.")
    return value


@router.get("/{public_id}/messages", response_model=MessagePage, summary="Archived group messages")
async def list_messages(
    public_id: str,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> MessagePage:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    after = _parse_after(cursor) if cursor else None
    page = await MessageService(session).list_messages(deal.id, after_id=after, limit=limit)
    next_cursor = encode_cursor({"i": page.rows[-1].id}) if page.has_more and page.rows else None
    return MessagePage(
        data=[MessagePublic.build(row) for row in page.rows],
        pagination={"limit": limit, "next_cursor": next_cursor, "has_more": page.has_more},
    )


@router.get(
    "/{public_id}/messages/{message_id}/revisions",
    response_model=RevisionPage,
    summary="Edit and deletion history of one archived message",
)
async def list_revisions(
    public_id: str,
    message_id: int,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> RevisionPage:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    revisions = await MessageService(session).list_revisions(deal.id, message_id)
    if revisions is None:
        raise NotFoundError("Message not found.")
    return RevisionPage(data=[RevisionPublic.build(row) for row in revisions])
