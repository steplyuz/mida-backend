"""Deal wire format. Money is integer tiyin. Internal database ids and Telegram identities
are never exposed; deals are addressed by their UUID public_id."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.models.audit import AuditLog
from app.models.deal import Deal
from app.models.enums import DealPaymentStatus, DealStatus, ParticipantRole, TelegramGroupStatus


class DealCreateRequest(BaseModel):
    # The caller always becomes one side of the deal; the other side is identified by Telegram id.
    model_config = ConfigDict(extra="forbid")

    my_role: Literal["buyer", "seller"]
    counterparty_telegram_user_id: StrictInt = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    product_amount_tiyin: StrictInt = Field(gt=0)
    terms_text: str = Field(min_length=1, max_length=10_000)


class DealUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class TermsPublic(BaseModel):
    terms_version: str
    terms_text: str
    service_fee_bps: int
    currency: str


class DealPublic(BaseModel):
    id: str
    deal_number: str
    title: str
    description: str | None
    buyer_user_id: int
    seller_user_id: int
    my_role: str
    product_amount_tiyin: int
    service_fee_bps: int
    service_fee_tiyin: int
    total_amount_tiyin: int
    currency: str
    status: DealStatus
    payment_status: DealPaymentStatus
    telegram_group_status: TelegramGroupStatus
    terms: TermsPublic
    version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def build(cls, deal: Deal, my_role: str) -> DealPublic:
        snapshot = json.loads(deal.terms_snapshot_json)
        return cls(
            id=deal.public_id,
            deal_number=deal.deal_number,
            title=deal.title,
            description=deal.description,
            buyer_user_id=deal.buyer_id,
            seller_user_id=deal.seller_id,
            my_role=my_role,
            product_amount_tiyin=deal.product_amount_tiyin,
            service_fee_bps=deal.service_fee_bps,
            service_fee_tiyin=deal.service_fee_tiyin,
            total_amount_tiyin=deal.total_amount_tiyin,
            currency=deal.currency,
            status=deal.status,
            payment_status=deal.payment_status,
            telegram_group_status=deal.telegram_group_status,
            terms=TermsPublic(**snapshot),
            version=deal.version,
            created_at=deal.created_at,
            updated_at=deal.updated_at,
            completed_at=deal.completed_at,
            cancelled_at=deal.cancelled_at,
        )


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class DealPage(BaseModel):
    data: list[DealPublic]
    pagination: PageInfo


class DealEventPublic(BaseModel):
    id: int
    action: str
    actor_kind: str
    actor_user_id: int | None
    old_values: dict[str, Any] | None
    new_values: dict[str, Any] | None
    created_at: datetime

    @classmethod
    def build(cls, log: AuditLog) -> DealEventPublic:
        return cls(
            id=log.id,
            action=log.action,
            actor_kind=log.actor_kind,
            actor_user_id=log.actor_user_id,
            old_values=json.loads(log.old_values_json) if log.old_values_json else None,
            new_values=json.loads(log.new_values_json) if log.new_values_json else None,
            created_at=log.created_at,
        )


class EventPage(BaseModel):
    data: list[DealEventPublic]
    pagination: PageInfo


class ParticipantPublic(BaseModel):
    user_id: int
    first_name: str
    role: ParticipantRole
    joined_at: datetime


class ParticipantPage(BaseModel):
    data: list[ParticipantPublic]
    pagination: PageInfo
