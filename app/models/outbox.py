from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, enum_column
from app.models.enums import OutboxStatus


class OutboxEvent(Base):
    """Transactional outbox. Written in the same transaction as the business change it describes.

    dedupe_key is unique, so the same logical work can never be enqueued twice.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[int] = mapped_column(Integer)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[OutboxStatus] = mapped_column(enum_column(OutboxStatus), default=OutboxStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
