"""Idempotency for state-changing endpoints.

The record is written in the same transaction as the business change it protects, so both commit
together or neither does. When two requests with the same key race, the loser hits the unique
constraint, rolls back, and is answered from the winner's committed record (a replay).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domain_errors import (
    ConcurrencyConflictError,
    IdempotencyDuplicateError,
    IdempotencyKeyConflictError,
    IdempotencyKeyError,
)
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.idempotency import IdempotencyRecord

_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")
RESOURCE_TYPE_DEAL = "deal"


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    user_id: int
    operation: str
    key: str
    request_hash: str


def validate_key(raw: str | None) -> str:
    if raw is None or _KEY_PATTERN.fullmatch(raw) is None:
        raise IdempotencyKeyError()
    return raw


def build_request_hash(operation: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"op": operation, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_same_request(record: IdempotencyRecord, request_hash: str) -> None:
    if record.request_hash != request_hash:
        raise IdempotencyKeyConflictError()


class IdempotencyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, user_id: int, operation: str, key: str) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == key,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def claim(self, claim: IdempotencyClaim, *, resource_id: int) -> None:
        """Must run inside the business transaction, after the resource id is known."""
        self._session.add(
            IdempotencyRecord(
                user_id=claim.user_id,
                operation=claim.operation,
                idempotency_key=claim.key,
                request_hash=claim.request_hash,
                resource_type=RESOURCE_TYPE_DEAL,
                resource_id=resource_id,
                created_at=utcnow(),
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise IdempotencyDuplicateError() from exc


async def run_idempotent(
    session: AsyncSession,
    *,
    user_id: int,
    operation: str,
    key: str,
    request_hash: str,
    load: Callable[[int], Awaitable[Deal]],
    action: Callable[[IdempotencyClaim], Awaitable[Deal]],
) -> tuple[Deal, bool]:
    """Returns (deal, replayed). A replay returns the current state of the original deal."""
    service = IdempotencyService(session)
    existing = await service.find(user_id, operation, key)
    if existing is not None:
        ensure_same_request(existing, request_hash)
        return await load(existing.resource_id), True

    claim = IdempotencyClaim(user_id, operation, key, request_hash)
    try:
        deal = await action(claim)
    except (IdempotencyDuplicateError, ConcurrencyConflictError):
        # Lost a race on the same key: the committed winner is the answer for this request.
        existing = await service.find(user_id, operation, key)
        if existing is None:
            raise
        ensure_same_request(existing, request_hash)
        return await load(existing.resource_id), True
    return deal, False
