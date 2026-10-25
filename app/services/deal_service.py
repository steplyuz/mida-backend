"""Deal use cases. Each mutation commits the business change, its audit event and, when given, its
idempotency record in one transaction."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor, ActorKind
from app.core.domain_errors import (
    ConcurrencyConflictError,
    InvalidTransitionError,
    InvariantViolationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.time import utcnow
from app.models.audit import AuditLog
from app.models.deal import Deal, DealParticipant
from app.models.enums import DealPaymentStatus, DealStatus, ParticipantRole, TelegramGroupStatus
from app.models.user import User
from app.repositories.audit_repository import AuditRepository
from app.repositories.deal_repository import DealRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.idempotency_service import IdempotencyClaim, IdempotencyService
from app.services.notification_service import enqueue_deal_created, enqueue_status_changed
from app.services.outbox_service import GROUP_EVENT, OutboxService
from app.services.pricing_service import PricingService
from app.services.state_machine import DealRole, check_transition

TERMS_VERSION = "v1"
MAX_TITLE_LENGTH = 200
MAX_TERMS_LENGTH = 10_000
MAX_DESCRIPTION_LENGTH = 2000
EDITABLE_STATUSES = frozenset({DealStatus.CREATED, DealStatus.AWAITING_PAYMENT})


@dataclass(frozen=True, slots=True)
class CreateDealInput:
    """Deliberately has no fee or total fields: the server computes them."""

    title: str
    buyer_id: int
    seller_id: int
    product_amount_tiyin: int
    terms_text: str
    description: str | None = None


def _normalize_title(raw: str) -> str:
    title = " ".join(raw.split())
    if not title:
        raise ValidationDomainError("Title is required.")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValidationDomainError(f"Title must be at most {MAX_TITLE_LENGTH} characters.")
    return title


class DealService:
    def __init__(self, session: AsyncSession, pricing: PricingService) -> None:
        self._session = session
        self._pricing = pricing
        self._deals = DealRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditService(session)
        self._audit_rows = AuditRepository(session)

    # ---------- reads and access ----------

    async def get_deal_by_public_id(self, actor: Actor, public_id: str) -> Deal:
        try:
            uuid.UUID(public_id)
        except ValueError:
            raise NotFoundError("Deal not found.") from None
        deal = await self._deals.get_by_public_id(public_id)
        if deal is None:
            raise NotFoundError("Deal not found.")
        await self._resolve_role(actor, deal)  # raises NotFound for non-participants
        return deal

    async def get_deal(self, actor: Actor, deal_id: int) -> Deal:
        deal = await self._require_deal(deal_id)
        await self._resolve_role(actor, deal)  # raises NotFound for non-participants
        return deal

    async def viewer_role(self, actor: Actor, deal: Deal) -> str:
        return (await self._resolve_role(actor, deal)).value

    async def list_deals(
        self,
        actor: Actor,
        *,
        status: DealStatus | None,
        descending: bool,
        after: tuple[datetime, int] | None,
        limit: int,
    ) -> tuple[list[tuple[Deal, str]], bool]:
        viewer = actor.user_id if actor.kind is ActorKind.USER else None
        rows = await self._deals.list_deals(
            viewer_user_id=viewer,
            status=status,
            descending=descending,
            after=after,
            limit=limit,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        result: list[tuple[Deal, str]] = []
        for deal, role in page:
            label = role.value if role is not None else actor.kind.value
            result.append((deal, label))
        return result, has_more

    async def list_events(
        self, actor: Actor, deal_id: int, *, after_id: int | None, limit: int
    ) -> tuple[list[AuditLog], bool]:
        deal = await self._require_deal(deal_id)
        await self._resolve_role(actor, deal)
        rows = await self._audit_rows.list_for_deal(deal.id, after_id=after_id, limit=limit + 1)
        return rows[:limit], len(rows) > limit

    async def list_participants(self, actor: Actor, deal_id: int) -> list[tuple[DealParticipant, User]]:
        deal = await self._require_deal(deal_id)
        await self._resolve_role(actor, deal)
        return await self._deals.list_participants(deal.id)

    # ---------- writes ----------

    async def create_deal(
        self,
        actor: Actor,
        data: CreateDealInput,
        *,
        idempotency: IdempotencyClaim | None = None,
    ) -> Deal:
        try:
            if actor.kind is ActorKind.USER and actor.user_id not in (
                data.buyer_id,
                data.seller_id,
            ):
                raise PermissionDeniedError("Only a deal party can create this deal.")
            title = _normalize_title(data.title)
            terms = data.terms_text.strip()
            if not terms or len(terms) > MAX_TERMS_LENGTH:
                raise ValidationDomainError("Terms are required and must be concise.")
            if data.buyer_id == data.seller_id:
                raise InvariantViolationError("Buyer and seller must be different users.")
            await self._require_active_user(data.buyer_id)
            await self._require_active_user(data.seller_id)

            price = self._pricing.calculate(data.product_amount_tiyin)
            deal_number = await self._deals.next_deal_number()
            now = utcnow()

            deal = await self._deals.add(
                Deal(
                    public_id=str(uuid.uuid4()),
                    deal_number=deal_number,
                    title=title,
                    description=data.description,
                    terms_snapshot_json=json.dumps(
                        {
                            "terms_version": TERMS_VERSION,
                            "terms_text": terms,
                            "service_fee_bps": price.service_fee_bps,
                            "currency": price.currency,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    buyer_id=data.buyer_id,
                    seller_id=data.seller_id,
                    product_amount_tiyin=price.product_amount_tiyin,
                    service_fee_bps=price.service_fee_bps,
                    service_fee_tiyin=price.service_fee_tiyin,
                    total_amount_tiyin=price.total_amount_tiyin,
                    currency=price.currency,
                    status=DealStatus.CREATED,
                    payment_status=DealPaymentStatus.PENDING,
                    telegram_group_status=TelegramGroupStatus.NOT_CREATED,
                    version=1,
                )
            )
            await self._deals.add_participant(deal.id, data.buyer_id, ParticipantRole.BUYER, now)
            await self._deals.add_participant(deal.id, data.seller_id, ParticipantRole.SELLER, now)
            await OutboxService(self._session).enqueue(
                event_type=GROUP_EVENT,
                aggregate_id=deal.id,
                dedupe_key=f"{GROUP_EVENT}:{deal.id}",
                payload={"deal_id": deal.id},
                now=now,
            )
            await enqueue_deal_created(
                self._session, deal, actor.user_id if actor.user_id is not None else data.buyer_id
            )
            if idempotency is not None:
                await IdempotencyService(self._session).claim(idempotency, resource_id=deal.id)

            self._audit.record(
                actor=actor,
                action="deal.created",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                new={
                    "deal_number": deal.deal_number,
                    "status": deal.status.value,
                    "product_amount_tiyin": deal.product_amount_tiyin,
                    "service_fee_bps": deal.service_fee_bps,
                    "service_fee_tiyin": deal.service_fee_tiyin,
                    "total_amount_tiyin": deal.total_amount_tiyin,
                },
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return deal

    async def update_details(
        self,
        actor: Actor,
        deal_id: int,
        *,
        expected_version: int,
        changes: dict[str, str | None],
    ) -> Deal:
        try:
            deal = await self._require_deal(deal_id)
            await self._resolve_role(actor, deal)
            if deal.version != expected_version:
                raise ConcurrencyConflictError()
            if deal.status not in EDITABLE_STATUSES:
                raise InvalidTransitionError("Details can no longer be changed once payment has progressed.")

            clean: dict[str, str | None] = {}
            if "title" in changes:
                clean["title"] = _normalize_title(changes["title"] or "")
            if "description" in changes:
                description = changes["description"]
                if description is not None and len(description) > MAX_DESCRIPTION_LENGTH:
                    raise ValidationDomainError(
                        f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters."
                    )
                clean["description"] = description
            if not clean:
                return deal

            old = {key: getattr(deal, key) for key in clean}
            if not await self._deals.update_details_if_version(deal.id, expected_version, clean, utcnow()):
                raise ConcurrencyConflictError()
            await self._session.refresh(deal)
            self._audit.record(
                actor=actor,
                action="deal.details_updated",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                old=old,
                new=clean,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return deal

    async def transition(
        self,
        actor: Actor,
        deal_id: int,
        target: DealStatus,
        *,
        expected_version: int,
        reason: str | None = None,
        idempotency: IdempotencyClaim | None = None,
    ) -> Deal:
        try:
            deal = await self._require_deal(deal_id)
            role = await self._resolve_role(actor, deal)
            if deal.version != expected_version:
                raise ConcurrencyConflictError()
            previous = deal.status
            check_transition(previous, target, role)

            if not await self._deals.update_status_if_version(deal.id, expected_version, target, utcnow()):
                raise ConcurrencyConflictError()
            await self._session.refresh(deal)
            if idempotency is not None:
                await IdempotencyService(self._session).claim(idempotency, resource_id=deal.id)

            self._audit.record(
                actor=actor,
                action="deal.status_changed",
                entity_type="deal",
                entity_id=deal.id,
                deal_id=deal.id,
                old={"status": previous.value, "version": expected_version},
                new={"status": deal.status.value, "version": deal.version, "reason": reason},
            )
            await enqueue_status_changed(self._session, deal, actor, deal.status)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return deal

    # ---------- internals ----------

    async def _require_deal(self, deal_id: int) -> Deal:
        deal = await self._deals.get_by_id(deal_id)
        if deal is None:
            raise NotFoundError("Deal not found.")
        return deal

    async def _require_active_user(self, user_id: int) -> None:
        user = await self._users.get(user_id)
        if user is None or not user.is_active:
            raise InvariantViolationError("Both parties must be active users.")

    async def _resolve_role(self, actor: Actor, deal: Deal) -> DealRole:
        if actor.kind is ActorKind.SYSTEM:
            return DealRole.SYSTEM
        if actor.kind is ActorKind.ADMIN:
            return DealRole.ADMIN
        participant = await self._deals.get_participant_role(deal.id, actor.user_id or 0)
        if participant is None:
            # Same response as a missing deal, so existence is not revealed.
            raise NotFoundError("Deal not found.")
        return DealRole(participant.value)
