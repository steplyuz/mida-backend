from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin
from app.db.types import UTCDateTime, enum_column
from app.models.enums import DisputeReason, DisputeResolution, DisputeStatus


class Dispute(TimestampMixin, Base):
    __tablename__ = "disputes"
    __table_args__ = (
        # At most one unresolved dispute per deal. Enforced by the database, not only by code.
        Index(
            "ux_one_open_dispute_per_deal",
            "deal_id",
            unique=True,
            sqlite_where=text("status <> 'resolved'"),
        ),
        UniqueConstraint("public_id", name="public_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), default=lambda: str(uuid.uuid4()))
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="RESTRICT"), index=True)
    opened_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason_code: Mapped[DisputeReason] = mapped_column(enum_column(DisputeReason))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[DisputeStatus] = mapped_column(enum_column(DisputeStatus), default=DisputeStatus.OPEN)
    assigned_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    resolution_code: Mapped[DisputeResolution | None] = mapped_column(enum_column(DisputeResolution))
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
