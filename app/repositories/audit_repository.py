from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


class AuditRepository:
    """Writes are insert-only by design: there are intentionally no update or delete methods."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, entry: AuditLog) -> None:
        self._session.add(entry)

    async def list_for_deal(self, deal_id: int, *, after_id: int | None, limit: int) -> list[AuditLog]:
        stmt = select(AuditLog).where(AuditLog.deal_id == deal_id)
        if after_id is not None:
            stmt = stmt.where(AuditLog.id > after_id)
        stmt = stmt.order_by(AuditLog.id.asc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())
