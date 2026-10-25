"""Seller payouts: the ledger of money MIDA owes a seller for a completed deal.

Eligibility: the deal is completed and its payment is confirmed. The amount is the product price; the
MIDA service fee stays with MIDA. Four-eyes at every step: the admin who requests cannot approve, and
the admin who requests cannot mark it paid. The bank transfer itself happens outside this system.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.domain_errors import (
    InvalidTransitionError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.enums import DealPaymentStatus, DealStatus, PayoutStatus
from app.models.payout import Payout
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService


def _text(value: str, low: int, high: int, what: str) -> str:
    cleaned = value.strip()
    if not low <= len(cleaned) <= high:
        raise ValidationDomainError(f"{what} must be {low} to {high} characters.")
    return cleaned


class PayoutService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._deals = DealRepository(session)
        self._audit = AuditService(session)

    async def list(self, status: PayoutStatus | None, limit: int) -> list[Payout]:
        stmt = select(Payout).order_by(Payout.id.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Payout.status == status)
        return list((await self._s.execute(stmt)).scalars().all())

    async def get(self, public_id: str) -> Payout:
        payout = (
            await self._s.execute(select(Payout).where(Payout.public_id == public_id))
        ).scalar_one_or_none()
        if payout is None:
            raise NotFoundError("Payout not found.")
        return payout

    async def request(self, actor: Actor, deal_public_id: str) -> Payout:
        try:
            deal = (
                await self._s.execute(select(Deal).where(Deal.public_id == deal_public_id))
            ).scalar_one_or_none()
            if deal is None:
                raise NotFoundError("Deal not found.")
            if deal.status is not DealStatus.COMPLETED:
                raise InvalidTransitionError("A payout is possible only for a completed deal.")
            if deal.payment_status is not DealPaymentStatus.CONFIRMED:
                raise InvariantViolationError("The buyer's payment is not confirmed.")
            existing = (
                await self._s.execute(select(Payout).where(Payout.deal_id == deal.id))
            ).scalar_one_or_none()
            now = utcnow()
            if existing is not None and existing.status is not PayoutStatus.REJECTED:
                raise InvalidTransitionError("A payout already exists for this deal.")
            if existing is not None:
                # A rejected request may be raised again, by a different administrator if they wish.
                existing.status = PayoutStatus.REQUESTED
                existing.requested_by = actor.user_id or 0
                existing.reject_reason = None
                existing.updated_at = now
                payout = existing
            else:
                payout = Payout(
                    deal_id=deal.id,
                    seller_user_id=deal.seller_id,
                    amount_tiyin=deal.product_amount_tiyin,
                    currency=deal.currency,
                    status=PayoutStatus.REQUESTED,
                    requested_by=actor.user_id or 0,
                    created_at=now,
                    updated_at=now,
                )
                self._s.add(payout)
            await self._s.flush()
            self._audit.record(
                actor=actor,
                action="payout.requested",
                entity_type="payout",
                entity_id=payout.id,
                deal_id=deal.id,
                new={"amount_tiyin": payout.amount_tiyin},
            )
            await self._s.commit()
        except IntegrityError:
            await self._s.rollback()
            raise InvalidTransitionError("A payout already exists for this deal.") from None
        except Exception:
            await self._s.rollback()
            raise
        return payout

    async def approve(self, actor: Actor, public_id: str) -> Payout:
        try:
            payout = await self.get(public_id)
            if payout.status is not PayoutStatus.REQUESTED:
                raise InvalidTransitionError("Only a requested payout can be approved.")
            if payout.requested_by == actor.user_id:
                raise PermissionDeniedError("A second administrator must approve this payout.")
            payout.status = PayoutStatus.APPROVED
            payout.approved_by = actor.user_id
            payout.approved_at = utcnow()
            payout.updated_at = payout.approved_at
            self._audit.record(
                actor=actor,
                action="payout.approved",
                entity_type="payout",
                entity_id=payout.id,
                deal_id=payout.deal_id,
                new={"status": "approved"},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payout

    async def reject(self, actor: Actor, public_id: str, *, reason: str) -> Payout:
        text = _text(reason, 3, 500, "Reason")
        try:
            payout = await self.get(public_id)
            if payout.status is not PayoutStatus.REQUESTED:
                raise InvalidTransitionError("Only a requested payout can be rejected.")
            payout.status = PayoutStatus.REJECTED
            payout.reject_reason = text
            payout.updated_at = utcnow()
            self._audit.record(
                actor=actor,
                action="payout.rejected",
                entity_type="payout",
                entity_id=payout.id,
                deal_id=payout.deal_id,
                new={"reason": text},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payout

    async def mark_paid(self, actor: Actor, public_id: str, *, bank_reference: str) -> Payout:
        reference = _text(bank_reference, 3, 128, "Bank reference")
        try:
            payout = await self.get(public_id)
            if payout.status is not PayoutStatus.APPROVED:
                raise InvalidTransitionError("Only an approved payout can be marked as paid.")
            if payout.requested_by == actor.user_id:
                raise PermissionDeniedError("The administrator who requested a payout cannot mark it paid.")
            reused = (
                await self._s.execute(select(Payout.id).where(Payout.bank_reference == reference))
            ).first()
            if reused is not None:
                raise InvariantViolationError("This bank reference is already used for another payout.")
            moment: datetime = utcnow()
            payout.status = PayoutStatus.PAID
            payout.paid_by = actor.user_id
            payout.paid_at = moment
            payout.bank_reference = reference
            payout.updated_at = moment
            self._audit.record(
                actor=actor,
                action="payout.paid",
                entity_type="payout",
                entity_id=payout.id,
                deal_id=payout.deal_id,
                new={"status": "paid", "reference_last4": reference[-4:]},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payout
