"""Manual payment verification. Maker-checker: the admin who records a bank transfer cannot approve it.

Money is never moved here. Approval means: the bank statement was checked by a second person and the
deal may proceed. The bank reference is stored so each transfer can be matched to one payment.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.domain_errors import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.time import utcnow
from app.models.deal import Deal
from app.models.enums import DealPaymentStatus, DealStatus, PaymentStatus
from app.models.payment import Payment
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService
from app.services.state_machine import DealRole, check_transition

MANUAL = "manual"


class AdminPaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._deals = DealRepository(session)
        self._audit = AuditService(session)

    async def list(self, status: PaymentStatus | None, limit: int) -> list[Payment]:
        stmt = select(Payment).order_by(Payment.id.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Payment.status == status)
        return list((await self._s.execute(stmt)).scalars().all())

    async def _load(self, payment_id: int) -> Payment:
        payment = await self._s.get(Payment, payment_id)
        if payment is None:
            raise NotFoundError("Payment not found.")
        return payment

    async def record(
        self, actor: Actor, payment_id: int, *, bank_reference: str, amount_tiyin: int
    ) -> Payment:
        reference = bank_reference.strip()
        if not 3 <= len(reference) <= 128:
            raise ValidationDomainError("Bank reference must be 3 to 128 characters.")
        try:
            payment = await self._load(payment_id)
            if payment.provider != MANUAL:
                raise InvariantViolationError("Only manual bank transfers are recorded by hand.")
            if payment.status is not PaymentStatus.REQUIRES_VERIFICATION:
                raise InvalidTransitionError("This payment is not waiting for verification.")
            if payment.verify_recorded_by is not None:
                raise InvalidTransitionError("A bank transfer is already recorded for this payment.")
            if amount_tiyin != payment.amount_tiyin:
                raise InvariantViolationError("The amount does not match the billed total.")
            duplicate = (
                await self._s.execute(select(Payment.id).where(Payment.verify_reference == reference))
            ).first()
            if duplicate is not None:
                # One bank reference must match one payment; reuse is a red flag, not a typo.
                raise InvariantViolationError("This bank reference is already recorded for another payment.")
            payment.verify_reference = reference
            payment.verify_recorded_by = actor.user_id
            payment.verify_recorded_at = utcnow()
            self._audit.record(
                actor=actor,
                action="payment.bank_recorded",
                entity_type="payment",
                entity_id=payment.id,
                deal_id=payment.deal_id,
                new={"reference_last4": reference[-4:]},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payment

    async def approve(self, actor: Actor, payment_id: int) -> Payment:
        try:
            payment = await self._load(payment_id)
            if (
                payment.status is not PaymentStatus.REQUIRES_VERIFICATION
                or payment.verify_recorded_by is None
            ):
                raise InvalidTransitionError("Nothing is recorded to approve for this payment.")
            if payment.verify_recorded_by == actor.user_id:
                raise PermissionDeniedError("A second administrator must approve this payment.")
            deal = await self._deals.get_by_id(payment.deal_id)
            if deal is None:
                raise NotFoundError("Deal not found.")
            moment = utcnow()
            await self._move_deal(deal, DealStatus.PAYMENT_CONFIRMED, actor, moment)
            payment.status = PaymentStatus.CONFIRMED
            payment.verified_at = moment
            payment.verify_approved_by = actor.user_id
            payment.verify_approved_at = moment
            deal.payment_status = DealPaymentStatus.CONFIRMED
            self._audit.record(
                actor=actor,
                action="payment.approved",
                entity_type="payment",
                entity_id=payment.id,
                deal_id=deal.id,
                new={"status": "confirmed"},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payment

    async def reject(self, actor: Actor, payment_id: int, *, reason: str) -> Payment:
        text = reason.strip()
        if not 3 <= len(text) <= 500:
            raise ValidationDomainError("Reason must be 3 to 500 characters.")
        try:
            payment = await self._load(payment_id)
            if payment.status is not PaymentStatus.REQUIRES_VERIFICATION:
                raise InvalidTransitionError("Only a payment waiting for verification can be rejected.")
            deal = await self._deals.get_by_id(payment.deal_id)
            if deal is None:
                raise NotFoundError("Deal not found.")
            if deal.status is DealStatus.PAYMENT_PENDING_VERIFICATION:
                await self._move_deal(deal, DealStatus.PAYMENT_FAILED, actor, utcnow())
            payment.status = PaymentStatus.FAILED
            deal.payment_status = DealPaymentStatus.FAILED
            self._audit.record(
                actor=actor,
                action="payment.rejected",
                entity_type="payment",
                entity_id=payment.id,
                deal_id=deal.id,
                new={"status": "failed", "reason": text},
            )
            await self._s.commit()
        except Exception:
            await self._s.rollback()
            raise
        return payment

    async def _move_deal(self, deal: Deal, target: DealStatus, actor: Actor, moment: datetime) -> None:
        previous = deal.status
        check_transition(previous, target, DealRole.SYSTEM)
        if not await self._deals.update_status_if_version(deal.id, deal.version, target, moment):
            raise ConcurrencyConflictError()
        await self._s.refresh(deal)
        self._audit.record(
            actor=actor,
            action="deal.status_changed",
            entity_type="deal",
            entity_id=deal.id,
            deal_id=deal.id,
            old={"status": previous.value},
            new={"status": target.value, "reason": "payment_review"},
        )
