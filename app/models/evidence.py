from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime, enum_column
from app.models.enums import EvidenceKind


class Evidence(Base):
    """Evidence metadata. File bytes live in storage under a server-generated key."""

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("public_id", name="public_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    dispute_id: Mapped[int | None] = mapped_column(ForeignKey("disputes.id", ondelete="RESTRICT"), index=True)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    kind: Mapped[EvidenceKind] = mapped_column(enum_column(EvidenceKind))
    label: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str | None] = mapped_column(String(64))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(127))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    message_ref_id: Mapped[int | None] = mapped_column(ForeignKey("deal_messages.id", ondelete="RESTRICT"))
    note_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
