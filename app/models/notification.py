from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class Notification(Base):
    """What the web app shows in its notification list.

    Written in the same transaction as the bot message it mirrors. The body never contains a
    Telegram invite link: links are one-use and are shown only on the deal page, to their owner.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_id", "user_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
