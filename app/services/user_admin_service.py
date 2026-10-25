"""User administration: search and block or unblock. Role changes are not offered over HTTP."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.domain_errors import InvalidTransitionError, NotFoundError, PermissionDeniedError
from app.models.user import User
from app.services.audit_service import AuditService


class UserAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._audit = AuditService(session)

    async def search(self, query: str | None, limit: int, offset: int) -> list[User]:
        stmt = select(User).order_by(User.id.desc()).limit(limit).offset(offset)
        if query:
            term = f"%{query.strip()}%"
            conditions: list[Any] = [User.first_name.ilike(term), User.telegram_username.ilike(term)]
            if query.strip().isdigit():
                conditions.append(User.id == int(query.strip()))
                conditions.append(User.telegram_user_id == int(query.strip()))
            stmt = stmt.where(or_(*conditions))
        return list((await self._s.execute(stmt)).scalars().all())

    async def set_active(self, actor: Actor, user_id: int, *, active: bool) -> User:
        user = await self._s.get(User, user_id)
        if user is None:
            raise NotFoundError("User not found.")
        if actor.user_id == user.id:
            raise PermissionDeniedError("You cannot block or unblock your own account.")
        if user.is_active == active:
            raise InvalidTransitionError("The account is already in that state.")
        user.is_active = active
        self._audit.record(
            actor=actor,
            action="user.unblocked" if active else "user.blocked",
            entity_type="user",
            entity_id=user.id,
            new={"is_active": active},
        )
        await self._s.commit()
        return user
