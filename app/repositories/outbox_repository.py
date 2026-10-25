from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OutboxStatus
from app.models.outbox import OutboxEvent


def _claimable(now: datetime) -> Any:
    """Pending and due, or processing with an expired lease (its worker crashed)."""
    return or_(
        and_(OutboxEvent.status == OutboxStatus.PENDING, OutboxEvent.next_attempt_at <= now),
        and_(OutboxEvent.status == OutboxStatus.PROCESSING, OutboxEvent.lease_expires_at < now),
    )


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, event: OutboxEvent) -> None:
        self._session.add(event)

    async def get(self, event_id: int) -> OutboxEvent | None:
        return await self._session.get(OutboxEvent, event_id)

    async def find_by_dedupe_key(self, dedupe_key: str) -> OutboxEvent | None:
        result = await self._session.execute(select(OutboxEvent).where(OutboxEvent.dedupe_key == dedupe_key))
        return result.scalar_one_or_none()

    async def due_ids(self, now: datetime, limit: int) -> list[int]:
        stmt = (
            select(OutboxEvent.id)
            .where(_claimable(now))
            .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def try_claim(self, event_id: int, *, now: datetime, lease_until: datetime, token: str) -> bool:
        """Compare-and-set: only one worker can win a given due event."""
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, _claimable(now))
            .values(
                status=OutboxStatus.PROCESSING,
                lease_token=token,
                lease_expires_at=lease_until,
                attempt_count=OutboxEvent.attempt_count + 1,
            )
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def finalize(self, event_id: int, token: str, values: dict[str, Any]) -> bool:
        """Writes the outcome only if this worker still holds the lease."""
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.lease_token == token)
            .values(lease_token=None, lease_expires_at=None, **values)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def reset_failed(self, event_id: int, now: datetime) -> bool:
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, OutboxEvent.status == OutboxStatus.FAILED)
            .values(
                status=OutboxStatus.PENDING,
                attempt_count=0,
                next_attempt_at=now,
                last_error_code=None,
                processed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1

    async def list_recent(self, status: OutboxStatus | None, limit: int) -> list[OutboxEvent]:
        stmt = select(OutboxEvent).order_by(OutboxEvent.id.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(OutboxEvent.status == status)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_by_status(self) -> dict[str, int]:
        stmt = select(OutboxEvent.status, func.count()).group_by(OutboxEvent.status)
        rows = (await self._session.execute(stmt)).all()
        return {str(status.value): int(count) for status, count in rows}
