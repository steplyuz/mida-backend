from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, enum_column
from app.models.enums import MessageSource, RevisionKind


class DealMessage(Base):
    """Archived group message. Text survives deletion. Each edit or deletion is a MessageRevision
    holding the text it replaced, so history is never silently overwritten."""

    __tablename__ = "deal_messages"
    __table_args__ = (UniqueConstraint("telegram_chat_id", "telegram_message_id", name="chat_message"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    source: Mapped[MessageSource] = mapped_column(enum_column(MessageSource))
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    sender_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    sender_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_display_name: Mapped[str | None] = mapped_column(String(255))
    message_type: Mapped[str] = mapped_column(String(32))
    text_content: Mapped[str | None] = mapped_column(Text)
    media_metadata_json: Mapped[str | None] = mapped_column(Text)
    telegram_message_date: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)


class MessageRevision(Base):
    __tablename__ = "message_revisions"
    __table_args__ = (UniqueConstraint("message_id", "revision_no", name="message_revision_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("deal_messages.id", ondelete="RESTRICT"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    change_kind: Mapped[RevisionKind] = mapped_column(enum_column(RevisionKind))
    previous_text: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class TelegramUpdateReceipt(Base):
    """One row per processed webhook update. A redelivered update_id is recognised and skipped."""

    __tablename__ = "telegram_update_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
