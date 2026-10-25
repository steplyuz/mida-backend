"""Enqueue, lease-based claim, and outcome recording for outbox events."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow
from app.models.enums import OutboxStatus
from app.models.outbox import OutboxEvent
from app.repositories.outbox_repository import OutboxRepository

GROUP_EVENT = "create_telegram_group"
AVATAR_EVENT = "set_group_avatar"
NOTIFY_EVENT = "notify_user"
GROUP_MESSAGE_EVENT = "group_message"


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    id: int
    event_type: str
    payload: dict[str, Any]
    attempt_count: int
    lease_token: str


class OutboxService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = OutboxRepository(session)

    async def enqueue(
        self,
        *,
        event_type: str,
        aggregate_id: int,
        dedupe_key: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> OutboxEvent:
        """Adds to the caller's transaction without committing. Duplicate key returns existing."""
        existing = await self._repo.find_by_dedupe_key(dedupe_key)
        if existing is not None:
            return existing
        moment = now or utcnow()
        event = OutboxEvent(
            event_type=event_type,
            aggregate_id=aggregate_id,
            dedupe_key=dedupe_key,
            payload_json=json.dumps(payload, sort_keys=True),
            status=OutboxStatus.PENDING,
            attempt_count=0,
            next_attempt_at=moment,
            created_at=moment,
        )
        self._repo.add(event)
        await self._session.flush()
        return event

    async def claim_due(self, *, now: datetime, lease_seconds: int, limit: int) -> list[ClaimedEvent]:
        claimed: list[ClaimedEvent] = []
        for event_id in await self._repo.due_ids(now, limit):
            token = uuid.uuid4().hex
            lease_until = now + timedelta(seconds=lease_seconds)
            if not await self._repo.try_claim(event_id, now=now, lease_until=lease_until, token=token):
                continue
            event = await self._repo.get(event_id)
            if event is None or event.lease_token != token:
                continue
            claimed.append(
                ClaimedEvent(
                    id=event.id,
                    event_type=event.event_type,
                    payload=json.loads(event.payload_json),
                    attempt_count=event.attempt_count,
                    lease_token=token,
                )
            )
        await self._session.commit()
        return claimed

    async def complete(self, event: ClaimedEvent, *, now: datetime) -> bool:
        ok = await self._repo.finalize(
            event.id, event.lease_token, {"status": OutboxStatus.DONE, "processed_at": now}
        )
        await self._session.commit()
        return ok

    async def reschedule(self, event: ClaimedEvent, *, next_attempt_at: datetime, error_code: str) -> bool:
        ok = await self._repo.finalize(
            event.id,
            event.lease_token,
            {
                "status": OutboxStatus.PENDING,
                "next_attempt_at": next_attempt_at,
                "last_error_code": error_code,
            },
        )
        await self._session.commit()
        return ok

    async def fail(self, event: ClaimedEvent, *, error_code: str, now: datetime) -> bool:
        ok = await self._repo.finalize(
            event.id,
            event.lease_token,
            {"status": OutboxStatus.FAILED, "last_error_code": error_code, "processed_at": now},
        )
        await self._session.commit()
        return ok

    async def admin_retry(self, event_id: int, *, now: datetime | None = None) -> bool:
        ok = await self._repo.reset_failed(event_id, now or utcnow())
        await self._session.commit()
        return ok

    async def get(self, event_id: int) -> OutboxEvent | None:
        return await self._repo.get(event_id)

    async def find_by_dedupe_key(self, dedupe_key: str) -> OutboxEvent | None:
        return await self._repo.find_by_dedupe_key(dedupe_key)

    async def list_recent(self, status: OutboxStatus | None, limit: int) -> list[OutboxEvent]:
        return await self._repo.list_recent(status, limit)

    async def count_by_status(self) -> dict[str, int]:
        return await self._repo.count_by_status()
