"""Disputes. A dispute and the deal move together in one transaction, with compare-and-set on both.

Resolution outcomes are recorded decisions. This service does not move money and does not
promise any legal effect: the record is the decision and its reason, nothing more.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor, ActorKind
from app.core.domain_errors import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.dispute import Dispute
from app.models.enums import DealStatus, DisputeReason, DisputeResolution, DisputeStatus
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService
from app.services.notification_service import enqueue_status_changed
from app.services.state_machine import DealRole, check_transition

MAX_DESCRIPTION = 2000
MIN_REASON = 20
MAX_REASON = 2000
RESOLUTION_TARGET = {
    DisputeResolution.BUYER_WINS: DealStatus.CANCELLED,
    DisputeResolution.SELLER_WINS: DealStatus.COMPLETED,
}


def _text(value: str, *, low: int, high: int, what: str) -> str:
    cleaned = value.strip()
    if not low <= len(cleaned) <= high:
        raise ValidationDomainError(f"{what} must be {low} to {high} characters.")
    return cleaned


class DisputeService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._deals = DealRepository(session)
        self._audit = AuditService(session)

    # ---------- reads ----------

    async def get(self, actor: Actor, public_id: str) -> tuple[Dispute, Deal]:
        try:
            uuid.UUID(public_id)
        except ValueError:
            raise NotFoundError("Dispute not found.") from None
        dispute = (
            await self._session.execute(select(Dispute).where(Dispute.public_id == public_id))
        ).scalar_one_or_none()
        if dispute is None:
            raise NotFoundError("Dispute not found.")
        deal = await self._deals.get_by_id(dispute.deal_id)
        if deal is None:
            raise NotFoundError("Dispute not found.")
        await self.viewer_role(actor, deal)
        return dispute, deal

    async def list_for_deal(self, actor: Actor, deal: Deal) -> list[Dispute]:
        await self.viewer_role(actor, deal)
        rows = await self._session.execute(
            select(Dispute).where(Dispute.deal_id == deal.id).order_by(Dispute.id)
        )
        return list(rows.scalars().all())

    async def viewer_role(self, actor: Actor, deal: Deal) -> DealRole:
        """Admins see every dispute; parties see their own deal's; others get not-found."""
        if actor.kind is ActorKind.ADMIN:
            return DealRole.ADMIN
        if actor.kind is not ActorKind.USER or actor.user_id is None:
            raise NotFoundError("Dispute not found.")
        role = await self._deals.get_participant_role(deal.id, actor.user_id)
        if role is None:
            raise NotFoundError("Dispute not found.")
        return DealRole(role.value)

    # ---------- writes ----------

    async def open(
        self,
        actor: Actor,
        deal_id: int,
        *,
        reason: DisputeReason,
        description: str,
        expected_version: int,
    ) -> Dispute:
        try:
            deal = await self._deals.get_by_id(deal_id)
            if deal is None:
                raise NotFoundError("Deal not found.")
            role = await self.viewer_role(actor, deal)
            if role not in (DealRole.BUYER, DealRole.SELLER):
                raise PermissionDeniedError("Only a deal party can open a dispute.")
            if deal.version != expected_version:
                raise ConcurrencyConflictError()
            check_transition(deal.status, DealStatus.DISPUTED, role)
            text = _text(description, low=1, high=MAX_DESCRIPTION, what="Description")
            if await self._open_dispute(deal.id) is not None:
                raise InvalidTransitionError("A dispute is already open for this deal.")

            moment = utcnow()
            if not await self._deals.update_status_if_version(
                deal.id, expected_version, DealStatus.DISPUTED, moment
            ):
                raise ConcurrencyConflictError()
            await self._session.refresh(deal)
            dispute = Dispute(
                public_id=str(uuid.uuid4()),
                deal_id=deal.id,
                opened_by=actor.user_id or 0,
                reason_code=reason,
                description=text,
                status=DisputeStatus.OPEN,
                version=1,
                created_at=moment,
                updated_at=moment,
            )
            self._session.add(dispute)
            await self._session.flush()
            self._audit.record(
                actor=actor,
                action="dispute.opened",
                entity_type="dispute",
                entity_id=dispute.id,
                deal_id=deal.id,
                new={"reason_code": reason.value, "status": "open"},
            )
            self._audit.record(
                actor=actor,
                action="deal.status_changed",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={"status": DealStatus.DISPUTED.value, "reason": "dispute"},
            )
            await enqueue_status_changed(self._session, deal, actor, DealStatus.DISPUTED)
            await self._session.commit()
        except IntegrityError:
            # The partial unique index caught a concurrent second dispute for the same deal.
            await self._session.rollback()
            raise InvalidTransitionError("A dispute is already open for this deal.") from None
        except Exception:
            await self._session.rollback()
            raise
        return dispute

    async def assign(self, actor: Actor, dispute: Dispute, *, expected_version: int) -> Dispute:
        if actor.kind is not ActorKind.ADMIN or actor.user_id is None:
            raise PermissionDeniedError("Administrator access required.")
        try:
            if dispute.status is DisputeStatus.UNDER_REVIEW and dispute.assigned_admin_id == actor.user_id:
                return dispute  # already yours; repeating the call is harmless
            if dispute.status is not DisputeStatus.OPEN:
                raise InvalidTransitionError("Only an open dispute can be assigned.")
            if dispute.version != expected_version:
                raise ConcurrencyConflictError()
            if not await self._cas_dispute(
                dispute.id,
                expected_version,
                {
                    "status": DisputeStatus.UNDER_REVIEW,
                    "assigned_admin_id": actor.user_id,
                    "updated_at": utcnow(),
                },
            ):
                raise ConcurrencyConflictError()
            await self._session.refresh(dispute)
            self._audit.record(
                actor=actor,
                action="dispute.assigned",
                entity_type="dispute",
                entity_id=dispute.id,
                deal_id=dispute.deal_id,
                new={"status": "under_review"},
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return dispute

    async def resolve(
        self,
        actor: Actor,
        dispute: Dispute,
        *,
        resolution: DisputeResolution,
        reason: str,
        expected_version: int,
    ) -> Dispute:
        if actor.kind is not ActorKind.ADMIN or actor.user_id is None:
            raise PermissionDeniedError("Administrator access required.")
        try:
            if dispute.status is not DisputeStatus.UNDER_REVIEW:
                raise InvalidTransitionError("Only a dispute under review can be resolved.")
            if dispute.assigned_admin_id != actor.user_id:
                raise PermissionDeniedError("Only the assigned administrator can resolve this dispute.")
            if dispute.version != expected_version:
                raise ConcurrencyConflictError()
            text = _text(reason, low=MIN_REASON, high=MAX_REASON, what="Resolution reason")

            deal = await self._deals.get_by_id(dispute.deal_id)
            if deal is None:
                raise NotFoundError("Deal not found.")
            target = RESOLUTION_TARGET[resolution]
            check_transition(deal.status, target, DealRole.ADMIN)

            moment = utcnow()
            if not await self._cas_dispute(
                dispute.id,
                expected_version,
                {
                    "status": DisputeStatus.RESOLVED,
                    "resolution_code": resolution,
                    "resolution_reason": text,
                    "resolved_by": actor.user_id,
                    "resolved_at": moment,
                    "updated_at": moment,
                },
            ):
                raise ConcurrencyConflictError()
            if not await self._deals.update_status_if_version(deal.id, deal.version, target, moment):
                raise ConcurrencyConflictError()
            await self._session.refresh(dispute)
            await self._session.refresh(deal)
            self._audit.record(
                actor=actor,
                action="dispute.resolved",
                entity_type="dispute",
                entity_id=dispute.id,
                deal_id=deal.id,
                new={"resolution": resolution.value, "reason": text},
            )
            self._audit.record(
                actor=actor,
                action="deal.status_changed",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={"status": target.value, "reason": "dispute_resolution"},
            )
            await enqueue_status_changed(self._session, deal, actor, target)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return dispute

    # ---------- internals ----------

    async def _open_dispute(self, deal_id: int) -> Dispute | None:
        return (
            await self._session.execute(
                select(Dispute).where(Dispute.deal_id == deal_id, Dispute.status != DisputeStatus.RESOLVED)
            )
        ).scalar_one_or_none()

    async def _cas_dispute(self, dispute_id: int, expected_version: int, values: dict[str, Any]) -> bool:
        stmt = (
            update(Dispute)
            .where(Dispute.id == dispute_id, Dispute.version == expected_version)
            .values(version=Dispute.version + 1, **values)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount == 1
