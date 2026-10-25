from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.types import UTCDateTime, enum_column
from app.models.enums import DealPaymentStatus, DealStatus, ParticipantRole, TelegramGroupStatus


class Deal(TimestampMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint("product_amount_tiyin > 0", name="product_amount_positive"),
        CheckConstraint("service_fee_tiyin >= 0", name="fee_non_negative"),
        CheckConstraint("service_fee_bps BETWEEN 0 AND 10000", name="fee_bps_range"),
        CheckConstraint(
            "total_amount_tiyin = product_amount_tiyin + service_fee_tiyin",
            name="total_matches_components",
        ),
        CheckConstraint("currency = 'UZS'", name="currency_uzs"),
        CheckConstraint("buyer_id <> seller_id", name="distinct_parties"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    deal_number: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    terms_snapshot_json: Mapped[str] = mapped_column(Text)

    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)

    # All money is integer tiyin (1 UZS = 100 tiyin). Never float.
    product_amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    service_fee_bps: Mapped[int] = mapped_column(Integer)
    service_fee_tiyin: Mapped[int] = mapped_column(BigInteger)
    total_amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="UZS")

    status: Mapped[DealStatus] = mapped_column(enum_column(DealStatus), default=DealStatus.CREATED)
    payment_status: Mapped[DealPaymentStatus] = mapped_column(
        enum_column(DealPaymentStatus), default=DealPaymentStatus.PENDING
    )

    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_chat_title: Mapped[str | None] = mapped_column(String(255))
    telegram_group_status: Mapped[TelegramGroupStatus] = mapped_column(
        enum_column(TelegramGroupStatus), default=TelegramGroupStatus.NOT_CREATED
    )

    version: Mapped[int] = mapped_column(Integer, default=1)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DealParticipant(Base):
    __tablename__ = "deal_participants"
    __table_args__ = (
        UniqueConstraint("deal_id", "user_id", name="deal_user"),
        UniqueConstraint("deal_id", "participant_role", name="deal_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    participant_role: Mapped[ParticipantRole] = mapped_column(enum_column(ParticipantRole))
    joined_at: Mapped[datetime] = mapped_column(UTCDateTime)
