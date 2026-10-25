from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.types import UTCDateTime, enum_column
from app.models.enums import PaymentStatus


class Payment(TimestampMixin, Base):
    """A payment attempt for a deal. Its status is NOT the deal status; the deal state machine
    moves only in response to verified provider facts."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("deal_id", "idempotency_key", name="deal_idempotency"),
        CheckConstraint("amount_tiyin > 0", name="amount_positive"),
        CheckConstraint("currency = 'UZS'", name="currency_uzs"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_transaction_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    checkout_url: Mapped[str | None] = mapped_column(String(512))
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="UZS")
    status: Mapped[PaymentStatus] = mapped_column(enum_column(PaymentStatus), default=PaymentStatus.PENDING)
    payment_method: Mapped[str | None] = mapped_column(String(32))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Manual verification: one admin records the bank reference, a different admin approves it.
    verify_reference: Mapped[str | None] = mapped_column(String(128))
    verify_recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    verify_recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verify_approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    verify_approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class PaymentWebhookEvent(Base):
    """Each accepted provider event. A repeated event_id is recognised and never applied twice."""

    __tablename__ = "payment_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="provider_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    provider_transaction_id: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
