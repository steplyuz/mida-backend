from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.models.enums import DealPaymentStatus, PaymentStatus
from app.models.payment import Payment


class PaymentInstructions(BaseModel):
    """Where and how the buyer transfers money. Only MIDA's own account is ever shown here."""

    bank_name: str
    account_number: str
    recipient_name: str
    reference: str
    amount_tiyin: int
    currency: str


class PaymentPublic(BaseModel):
    """checkout_url is shown to the buyer only. Provider transaction ids are never exposed."""

    id: int
    status: PaymentStatus
    amount_tiyin: int
    currency: str
    checkout_url: str | None
    created_at: datetime
    verified_at: datetime | None
    instructions: PaymentInstructions | None = None

    @classmethod
    def build(cls, payment: Payment, *, is_buyer: bool) -> PaymentPublic:
        return cls(
            id=payment.id,
            status=payment.status,
            amount_tiyin=payment.amount_tiyin,
            currency=payment.currency,
            checkout_url=payment.checkout_url if is_buyer else None,
            created_at=payment.created_at,
            verified_at=payment.verified_at,
        )


class PaymentList(BaseModel):
    data: list[PaymentPublic]


class PaymentStatusPublic(BaseModel):
    deal_payment_status: DealPaymentStatus
    latest_payment_status: PaymentStatus | None


class ReconciliationPublic(BaseModel):
    data: list[dict[str, object]]
    mismatches: int
