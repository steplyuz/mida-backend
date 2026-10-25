from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, enum_column
from app.models.enums import MembershipKind


class TelegramMembership(Base):
    """How each deal party reached the group: added directly, or via a one-use invite link.

    invite_link is visible only to the party it was issued to, and is never logged.
    """

    __tablename__ = "telegram_memberships"
    __table_args__ = (UniqueConstraint("deal_id", "user_id", name="deal_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[MembershipKind] = mapped_column(enum_column(MembershipKind))
    invite_link: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
