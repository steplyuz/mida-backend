from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, enum_column
from app.models.enums import InquiryStatus


class Inquiry(Base):
    """A message to the MIDA team: from the public site (anonymous) or from a signed-in user."""

    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    contact: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[InquiryStatus] = mapped_column(enum_column(InquiryStatus), default=InquiryStatus.NEW)
    assigned_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class InquiryMessage(Base):
    __tablename__ = "inquiry_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.id", ondelete="RESTRICT"), index=True)
    author_kind: Mapped[str] = mapped_column(String(16))  # visitor | user | admin
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
