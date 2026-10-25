from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class NotificationPublic(BaseModel):
    id: int
    kind: str
    body: str
    # Public id of the deal, for opening it. Null for notifications that are not about a deal.
    deal_id: str | None
    created_at: datetime
    read: bool


class NotificationPagination(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class NotificationPage(BaseModel):
    data: list[NotificationPublic]
    unread_count: int
    pagination: NotificationPagination


class ReadAllResult(BaseModel):
    updated: int
