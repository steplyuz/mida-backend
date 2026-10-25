"""Single source of truth for 'now'. All stored timestamps are timezone-aware UTC."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
