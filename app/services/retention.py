"""Retention jobs. Each is an explicit call with an explicit cutoff: nothing deletes on its own.

Not covered on purpose:
- audit_logs: append-only by a database trigger. Retention for audit is a legal decision.
- deal_messages and evidence: personal data with no retention policy yet; see the README.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OutboxStatus
from app.models.idempotency import IdempotencyRecord
from app.models.outbox import OutboxEvent


async def purge_idempotency_records(session: AsyncSession, *, older_than: datetime) -> int:
    """Records must outlive client retries. Callers should use a cutoff of at least a day."""
    result = cast(
        CursorResult[Any],
        await session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < older_than)),
    )
    await session.commit()
    return int(result.rowcount or 0)


async def purge_completed_outbox_events(session: AsyncSession, *, older_than: datetime) -> int:
    """Only finished work. Failed events stay until an administrator has looked at them."""
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.status == OutboxStatus.DONE,
                OutboxEvent.processed_at < older_than,
            )
        ),
    )
    await session.commit()
    return int(result.rowcount or 0)
