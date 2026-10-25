from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.types import UTCDateTime


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
