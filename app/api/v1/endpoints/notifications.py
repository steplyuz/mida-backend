"""Web app notification list. Each user sees only their own rows."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Query
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from app.api.deps import CurrentUserDep, SessionDep
from app.core.domain_errors import NotFoundError, ValidationDomainError
from app.core.pagination import decode_cursor, encode_cursor
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.notification import Notification
from app.schemas.notification import (
    NotificationPage,
    NotificationPagination,
    NotificationPublic,
    ReadAllResult,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _public(row: Notification, deal_public_id: str | None) -> NotificationPublic:
    return NotificationPublic(
        id=row.id,
        kind=row.kind,
        body=row.body,
        deal_id=deal_public_id,
        created_at=row.created_at,
        read=row.read_at is not None,
    )


@router.get("", response_model=NotificationPage)
async def list_notifications(
    current: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
) -> NotificationPage:
    query = (
        select(Notification, Deal.public_id)
        .outerjoin(Deal, Deal.id == Notification.deal_id)
        .where(Notification.user_id == current.user.id)
        .order_by(Notification.id.desc())
    )
    if cursor:
        before = decode_cursor(cursor).get("before")
        if not isinstance(before, int):
            raise ValidationDomainError("Invalid pagination cursor.")
        query = query.where(Notification.id < before)

    rows = (await session.execute(query.limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor({"before": rows[-1][0].id}) if has_more and rows else None

    unread = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == current.user.id, Notification.read_at.is_(None)
        )
    )
    return NotificationPage(
        data=[_public(row, deal_public_id) for row, deal_public_id in rows],
        unread_count=int(unread or 0),
        pagination=NotificationPagination(limit=limit, next_cursor=next_cursor, has_more=has_more),
    )


@router.post("/{notification_id}/read", response_model=NotificationPublic)
async def mark_read(notification_id: int, current: CurrentUserDep, session: SessionDep) -> NotificationPublic:
    row = (
        await session.execute(
            select(Notification, Deal.public_id)
            .outerjoin(Deal, Deal.id == Notification.deal_id)
            .where(
                Notification.id == notification_id,
                Notification.user_id == current.user.id,
            )
        )
    ).one_or_none()
    if row is None:
        # Same answer whether the notification is missing or belongs to someone else.
        raise NotFoundError("Notification not found.")
    notification, deal_public_id = row
    if notification.read_at is None:
        notification.read_at = utcnow()
        await session.commit()
    return _public(notification, deal_public_id)


@router.post("/read-all", response_model=ReadAllResult)
async def mark_all_read(current: CurrentUserDep, session: SessionDep) -> ReadAllResult:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Notification)
            .where(Notification.user_id == current.user.id, Notification.read_at.is_(None))
            .values(read_at=utcnow())
        ),
    )
    await session.commit()
    return ReadAllResult(updated=int(result.rowcount or 0))
