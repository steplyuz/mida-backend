from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.counter import Counter
from app.models.deal import Deal, DealParticipant
from app.models.enums import DealStatus, ParticipantRole
from app.models.user import User

DEAL_NUMBER_COUNTER = "deal_number"


def format_deal_number(value: int) -> str:
    return f"MIDA-{value:06d}"


class DealRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, deal: Deal) -> Deal:
        self._session.add(deal)
        await self._session.flush()
        return deal

    async def get_by_id(self, deal_id: int) -> Deal | None:
        return await self._session.get(Deal, deal_id)

    async def get_by_public_id(self, public_id: str) -> Deal | None:
        result = await self._session.execute(select(Deal).where(Deal.public_id == public_id))
        return result.scalar_one_or_none()

    async def get_participant_role(self, deal_id: int, user_id: int) -> ParticipantRole | None:
        stmt = select(DealParticipant.participant_role).where(
            DealParticipant.deal_id == deal_id,
            DealParticipant.user_id == user_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add_participant(
        self, deal_id: int, user_id: int, role: ParticipantRole, joined_at: datetime
    ) -> None:
        self._session.add(
            DealParticipant(
                deal_id=deal_id,
                user_id=user_id,
                participant_role=role,
                joined_at=joined_at,
            )
        )
        await self._session.flush()

    async def list_participants(self, deal_id: int) -> list[tuple[DealParticipant, User]]:
        stmt = (
            select(DealParticipant, User)
            .join(User, User.id == DealParticipant.user_id)
            .where(DealParticipant.deal_id == deal_id)
            .order_by(DealParticipant.id)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(participant, user) for participant, user in rows]

    async def list_deals(
        self,
        *,
        viewer_user_id: int | None,
        status: DealStatus | None,
        descending: bool,
        after: tuple[datetime, int] | None,
        limit: int,
    ) -> list[tuple[Deal, ParticipantRole | None]]:
        """viewer_user_id=None means unrestricted (administrator). Rows are fetched with limit+1."""
        if viewer_user_id is None:
            stmt_all = select(Deal)
            rows_all = (
                (await self._session.execute(self._ordered(stmt_all, status, descending, after, limit)))
                .scalars()
                .all()
            )
            return [(deal, None) for deal in rows_all]

        stmt = (
            select(Deal, DealParticipant.participant_role)
            .join(DealParticipant, DealParticipant.deal_id == Deal.id)
            .where(DealParticipant.user_id == viewer_user_id)
        )
        rows = (await self._session.execute(self._ordered(stmt, status, descending, after, limit))).all()
        return [(deal, role) for deal, role in rows]

    @staticmethod
    def _ordered(
        stmt: Any,
        status: DealStatus | None,
        descending: bool,
        after: tuple[datetime, int] | None,
        limit: int,
    ) -> Any:
        if status is not None:
            stmt = stmt.where(Deal.status == status)
        if after is not None:
            cursor_at, cursor_id = after
            if descending:
                stmt = stmt.where(
                    or_(
                        Deal.created_at < cursor_at,
                        and_(Deal.created_at == cursor_at, Deal.id < cursor_id),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        Deal.created_at > cursor_at,
                        and_(Deal.created_at == cursor_at, Deal.id > cursor_id),
                    )
                )
        if descending:
            order = (Deal.created_at.desc(), Deal.id.desc())
        else:
            order = (Deal.created_at.asc(), Deal.id.asc())
        return stmt.order_by(*order).limit(limit + 1)

    async def next_deal_number(self) -> str:
        """Atomic increment inside the caller's transaction. SQLite serialises the write."""
        stmt = (
            update(Counter)
            .where(Counter.name == DEAL_NUMBER_COUNTER)
            .values(value=Counter.value + 1)
            .returning(Counter.value)
        )
        value = (await self._session.execute(stmt)).scalar_one()
        return format_deal_number(value)

    async def update_status_if_version(
        self,
        deal_id: int,
        expected_version: int,
        target: DealStatus,
        now: datetime,
    ) -> bool:
        """Compare-and-swap on version. False means another writer changed the deal first."""
        values: dict[str, Any] = {"status": target, "version": Deal.version + 1, "updated_at": now}
        if target is DealStatus.COMPLETED:
            values["completed_at"] = now
        if target is DealStatus.CANCELLED:
            values["cancelled_at"] = now
        return await self._cas_update(deal_id, expected_version, values)

    async def update_details_if_version(
        self,
        deal_id: int,
        expected_version: int,
        changes: dict[str, Any],
        now: datetime,
    ) -> bool:
        values: dict[str, Any] = {**changes, "version": Deal.version + 1, "updated_at": now}
        return await self._cas_update(deal_id, expected_version, values)

    async def _cas_update(self, deal_id: int, expected_version: int, values: dict[str, Any]) -> bool:
        stmt = (
            update(Deal)
            .where(Deal.id == deal_id, Deal.version == expected_version)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1
