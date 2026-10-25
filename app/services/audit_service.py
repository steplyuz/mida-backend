"""Writes audit events in the caller's transaction so business change and audit commit together."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.logging import request_id_ctx
from app.core.time import utcnow
from app.models.audit import AuditLog
from app.repositories.audit_repository import AuditRepository


def _dump(values: dict[str, Any] | None) -> str | None:
    if values is None:
        return None
    return json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    def record(
        self,
        *,
        actor: Actor,
        action: str,
        entity_type: str,
        entity_id: int | str,
        deal_id: int | None = None,
        old: dict[str, Any] | None = None,
        new: dict[str, Any] | None = None,
    ) -> None:
        self._repo.add(
            AuditLog(
                actor_user_id=actor.user_id,
                actor_kind=actor.kind.value,
                deal_id=deal_id,
                entity_type=entity_type,
                entity_id=str(entity_id),
                action=action,
                old_values_json=_dump(old),
                new_values_json=_dump(new),
                correlation_id=request_id_ctx.get(),
                created_at=utcnow(),
            )
        )
