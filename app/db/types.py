"""Custom column types: timezone-aware UTC datetimes and string-backed enums."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Rejects naive datetimes on write; always returns aware UTC on read (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be stored; use an aware UTC datetime")
        converted: datetime = value.astimezone(UTC)
        return converted

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            restored: datetime = value.replace(tzinfo=UTC)
            return restored
        converted: datetime = value.astimezone(UTC)
        return converted


def enum_column[E: StrEnum](enum_cls: type[E], *, length: int = 32) -> SAEnum:
    """Stores enum values (lowercase strings) with a CHECK constraint on the allowed set."""
    return SAEnum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )
