"""Payments. The deal state machine moves only on provider facts that passed signature checks.

Starting a payment has three phases, so the provider is never called inside a DB transaction:
  A. (txn)  record a PENDING payment with the client's key; replay returns the existing row.
  B. (none) call the provider with that same key; the provider dedupes on it.
  C. (txn)  store the provider transaction id and move the deal to payment_pending_verification.
If the process dies after B, the next request with the same key resumes at C.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor, ActorKind
from app.core.domain_errors import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    PaymentProviderError,
    PermissionDeniedError,
    ServiceNotConfiguredError,
)
from app.core.time import utcnow
from app.integrations.payments.base import PaymentProvider, ProviderEvent, ProviderStatus
from app.models.deal import Deal
from app.models.enums import DealPaymentStatus, DealStatus, PaymentStatus
from app.models.payment import Payment, PaymentWebhookEvent
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService
from app.services.notification_service import enqueue_status_changed
from app.services.state_machine import DealRole, check_transition

OPEN_STATUSES = frozenset({PaymentStatus.PENDING, PaymentStatus.REQUIRES_VERIFICATION})
# Payment status moves. Anything not listed is a conflict and is flagged, never applied.
PAYMENT_MOVES: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset({PaymentStatus.REQUIRES_VERIFICATION, PaymentStatus.FAILED}),
    PaymentStatus.REQUIRES_VERIFICATION: frozenset({PaymentStatus.CONFIRMED, PaymentStatus.FAILED}),
    PaymentStatus.CONFIRMED: frozenset({PaymentStatus.REFUND_PENDING}),
    PaymentStatus.REFUND_PENDING: frozenset({PaymentStatus.REFUNDED, PaymentStatus.PARTIALLY_REFUNDED}),
}


def payment_move_allowed(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in PAYMENT_MOVES.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    payment_id: int
    local_status: str
    provider_status: str | None
    mismatch: bool


class PaymentService:
    def __init__(self, session: AsyncSession, provider: PaymentProvider | None) -> None:
        self._session = session
        self._provider = provider
        self._deals = DealRepository(session)
        self._audit = AuditService(session)

    # ---------- starting a payment ----------

    async def start_payment(self, actor: Actor, deal_id: int, key: str) -> tuple[Payment, bool]:
        """Returns (payment, replayed)."""
        self._require_provider()
        payment, replayed = await self._prepare(actor, deal_id, key)
        if payment.provider_transaction_id is None:
            payment = await self._call_provider_and_record(payment)
        return payment, replayed

    def _require_provider(self) -> PaymentProvider:
        if self._provider is None:
            raise ServiceNotConfiguredError("No payment provider is configured.")
        return self._provider

    async def _prepare(self, actor: Actor, deal_id: int, key: str) -> tuple[Payment, bool]:
        provider = self._require_provider()
        try:
            deal = await self._deals.get_by_id(deal_id)
            if deal is None:
                raise NotFoundError("Deal not found.")
            if actor.kind is not ActorKind.USER or actor.user_id != deal.buyer_id:
                raise PermissionDeniedError("Only the buyer can pay for this deal.")

            existing = await self._by_key(deal.id, key)
            if existing is not None:
                return existing, True

            in_flight = (
                (
                    await self._session.execute(
                        select(Payment).where(Payment.deal_id == deal.id, Payment.status.in_(OPEN_STATUSES))
                    )
                )
                .scalars()
                .first()
            )
            if in_flight is not None:
                raise InvalidTransitionError("A payment for this deal is already in progress.")

            if deal.status in (DealStatus.CREATED, DealStatus.PAYMENT_FAILED):
                await self._move_deal(deal, DealStatus.AWAITING_PAYMENT, Actor.system())
            elif deal.status is not DealStatus.AWAITING_PAYMENT:
                raise InvalidTransitionError("Payment cannot be started in the current deal state.")

            payment = Payment(
                deal_id=deal.id,
                provider=provider.name,
                idempotency_key=key,
                amount_tiyin=deal.total_amount_tiyin,
                currency=deal.currency,
                status=PaymentStatus.PENDING,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            self._session.add(payment)
            await self._session.flush()
            await self._session.commit()
            return payment, False
        except (IntegrityError, ConcurrencyConflictError):
            # A concurrent request with the same key won the race (unique key, or the deal was
            # moved first). The winner's committed row is the answer for this caller too.
            await self._session.rollback()
            winner = await self._by_key(deal_id, key)
            if winner is None:
                raise
            return winner, True
        except Exception:
            await self._session.rollback()
            raise

    async def _call_provider_and_record(self, payment: Payment) -> Payment:
        provider = self._require_provider()
        # Captured before any rollback: attributes of a rolled-back ORM object cannot be read here.
        payment_id = payment.id
        deal = await self._deals.get_by_id(payment.deal_id)
        if deal is None:
            raise NotFoundError("Deal not found.")
        try:
            intent = await provider.create_payment(
                idempotency_key=payment.idempotency_key,
                amount_tiyin=payment.amount_tiyin,
                currency=payment.currency,
                description=f"MIDA {deal.deal_number}",
            )
        except PaymentProviderError:
            raise
        except Exception as exc:
            # Nothing was recorded, so the same key can safely retry later.
            raise PaymentProviderError() from exc

        try:
            row = await self._session.get(Payment, payment.id)
            if row is None:
                raise NotFoundError("Payment not found.")
            if row.provider_transaction_id is not None:
                # A concurrent request with the same key already recorded the transaction.
                await self._session.rollback()
                return row
            row.provider_transaction_id = intent.provider_transaction_id
            row.checkout_url = intent.checkout_url
            row.status = PaymentStatus.REQUIRES_VERIFICATION
            row.updated_at = utcnow()
            current = await self._deals.get_by_id(row.deal_id)
            if current is not None and current.status is DealStatus.AWAITING_PAYMENT:
                await self._move_deal(current, DealStatus.PAYMENT_PENDING_VERIFICATION, Actor.system())
                current.payment_status = DealPaymentStatus.REQUIRES_VERIFICATION
            else:
                # The deal moved on while the provider call was in flight. Keep the transaction
                # visible to reconciliation instead of silently dropping it.
                self._audit.record(
                    actor=Actor.system(),
                    action="payment.created_on_closed_deal",
                    entity_type="payment",
                    entity_id=row.id,
                    deal_id=row.deal_id,
                    new={"provider_transaction_id": row.provider_transaction_id},
                )
            self._audit.record(
                actor=Actor.system(),
                action="payment.started",
                entity_type="payment",
                entity_id=row.id,
                deal_id=row.deal_id,
                new={"status": row.status.value},
            )
            await self._session.commit()
            return row
        except ConcurrencyConflictError:
            # A concurrent recorder moved the deal first. Its committed row is authoritative.
            await self._session.rollback()
            peer = await self._session.get(Payment, payment_id)
            if peer is not None and peer.provider_transaction_id is not None:
                return peer
            raise
        except Exception:
            await self._session.rollback()
            raise

    # ---------- applying provider facts ----------

    async def apply_event(self, event: ProviderEvent) -> str:
        """Applies one verified provider event. Returns a short outcome label."""
        provider = self._require_provider()
        moment = utcnow()
        try:
            self._session.add(
                PaymentWebhookEvent(
                    provider=provider.name,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    provider_transaction_id=event.provider_transaction_id,
                    received_at=moment,
                )
            )
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return "duplicate"

            payment = (
                await self._session.execute(
                    select(Payment).where(
                        Payment.provider == provider.name,
                        Payment.provider_transaction_id == event.provider_transaction_id,
                    )
                )
            ).scalar_one_or_none()
            if payment is None:
                await self._session.commit()
                return "unknown_transaction"

            outcome = await self._apply_to_payment(payment, event, moment)
            await self._session.commit()
            return outcome
        except Exception:
            await self._session.rollback()
            raise

    async def _apply_to_payment(self, payment: Payment, event: ProviderEvent, moment: datetime) -> str:
        deal = await self._deals.get_by_id(payment.deal_id)
        if deal is None:
            return "unknown_transaction"

        if event.amount_tiyin != payment.amount_tiyin or event.currency != payment.currency:
            # Never trust a provider amount that differs from what was billed. Hold for review.
            self._flag(
                payment,
                "amount_mismatch",
                {"expected": payment.amount_tiyin, "received": event.amount_tiyin},
            )
            return "flagged"

        if event.event_type == "payment.succeeded":
            if payment.status is PaymentStatus.CONFIRMED:
                return "unchanged"
            if deal.status is not DealStatus.PAYMENT_PENDING_VERIFICATION or not payment_move_allowed(
                payment.status, PaymentStatus.CONFIRMED
            ):
                self._flag(payment, "out_of_order", {"deal_status": deal.status.value})
                return "flagged"
            payment.status = PaymentStatus.CONFIRMED
            payment.verified_at = moment
            await self._move_deal(deal, DealStatus.PAYMENT_CONFIRMED, Actor.system())
            deal.payment_status = DealPaymentStatus.CONFIRMED
            self._audit.record(
                actor=Actor.system(),
                action="payment.confirmed",
                entity_type="payment",
                entity_id=payment.id,
                deal_id=deal.id,
                new={"status": "confirmed"},
            )
            return "confirmed"

        # payment.failed
        if payment.status is PaymentStatus.FAILED:
            return "unchanged"
        if not payment_move_allowed(payment.status, PaymentStatus.FAILED):
            self._flag(payment, "conflicting_event", {"local_status": payment.status.value})
            return "flagged"
        payment.status = PaymentStatus.FAILED
        if deal.status is DealStatus.PAYMENT_PENDING_VERIFICATION:
            await self._move_deal(deal, DealStatus.PAYMENT_FAILED, Actor.system())
        deal.payment_status = DealPaymentStatus.FAILED
        self._audit.record(
            actor=Actor.system(),
            action="payment.failed",
            entity_type="payment",
            entity_id=payment.id,
            deal_id=deal.id,
            new={"status": "failed"},
        )
        return "failed"

    # ---------- reads and reconciliation ----------

    async def list_for_deal(self, deal_id: int) -> list[Payment]:
        rows = await self._session.execute(
            select(Payment).where(Payment.deal_id == deal_id).order_by(Payment.id)
        )
        return list(rows.scalars().all())

    async def reconcile(self, *, limit: int = 100) -> list[ReconciliationItem]:
        """Compares open payments with the provider. Reports only; it never changes state itself."""
        provider = self._require_provider()
        rows = (
            (
                await self._session.execute(
                    select(Payment)
                    .where(
                        Payment.provider == provider.name,
                        Payment.status.in_(OPEN_STATUSES),
                        Payment.provider_transaction_id.is_not(None),
                    )
                    .order_by(Payment.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items: list[ReconciliationItem] = []
        for payment in rows:
            tx = payment.provider_transaction_id
            if tx is None:
                continue
            remote = await provider.get_payment_status(tx)
            mismatch = _mismatch(payment.status, remote)
            items.append(
                ReconciliationItem(
                    payment_id=payment.id,
                    local_status=payment.status.value,
                    provider_status=remote.value,
                    mismatch=mismatch,
                )
            )
        return items

    # ---------- internals ----------

    async def _by_key(self, deal_id: int, key: str) -> Payment | None:
        return (
            await self._session.execute(
                select(Payment).where(Payment.deal_id == deal_id, Payment.idempotency_key == key)
            )
        ).scalar_one_or_none()

    async def _move_deal(self, deal: Deal, target: DealStatus, actor: Actor) -> None:
        previous = deal.status
        check_transition(previous, target, DealRole.SYSTEM)
        if not await self._deals.update_status_if_version(deal.id, deal.version, target, utcnow()):
            raise ConcurrencyConflictError()
        await self._session.refresh(deal)
        self._audit.record(
            actor=actor,
            action="deal.status_changed",
            entity_type="deal",
            entity_id=deal.id,
            deal_id=deal.id,
            old={"status": previous.value},
            new={"status": target.value, "reason": "payment"},
        )
        await enqueue_status_changed(self._session, deal, actor, target)

    def _flag(self, payment: Payment, reason: str, details: dict[str, Any]) -> None:
        if payment.status is PaymentStatus.PENDING or payment.status is PaymentStatus.REQUIRES_VERIFICATION:
            payment.status = PaymentStatus.REQUIRES_VERIFICATION
        self._audit.record(
            actor=Actor.system(),
            action=f"payment.flagged.{reason}",
            entity_type="payment",
            entity_id=payment.id,
            deal_id=payment.deal_id,
            new=details,
        )


def _mismatch(local: PaymentStatus, remote: ProviderStatus) -> bool:
    if remote is ProviderStatus.SUCCEEDED:
        return local is not PaymentStatus.CONFIRMED
    if remote is ProviderStatus.FAILED:
        return local is not PaymentStatus.FAILED
    return False
