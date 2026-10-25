"""Inquiries (murojaatlar) and support chat threads.

Public site visitors send an inquiry anonymously; they cannot read replies in this version, so the admin
answers through the contact they gave. Signed-in users (Mini App) see and continue their own threads.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.domain_errors import InvalidTransitionError, NotFoundError, ValidationDomainError
from app.core.time import utcnow
from app.models.enums import InquiryStatus
from app.models.inquiry import Inquiry, InquiryMessage
from app.services.audit_service import AuditService

SOURCES = frozenset({"web_contact", "web_partnership", "miniapp"})


def _clean(value: str, low: int, high: int, what: str) -> str:
    text = value.strip()
    if not low <= len(text) <= high:
        raise ValidationDomainError(f"{what} must be {low} to {high} characters.")
    return text


class InquiryService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self._audit = AuditService(session)

    async def create(
        self,
        *,
        name: str,
        contact: str,
        subject: str,
        body: str,
        source: str,
        user_id: int | None = None,
    ) -> Inquiry:
        if source not in SOURCES:
            raise ValidationDomainError("Unknown inquiry source.")
        now: datetime = utcnow()
        inquiry = Inquiry(
            public_id=str(uuid.uuid4()),
            user_id=user_id,
            name=_clean(name, 2, 120, "Name"),
            contact=_clean(contact, 3, 200, "Contact"),
            subject=_clean(subject, 3, 200, "Subject"),
            source=source,
            status=InquiryStatus.NEW,
            created_at=now,
            updated_at=now,
        )
        self._s.add(inquiry)
        await self._s.flush()
        self._s.add(
            InquiryMessage(
                inquiry_id=inquiry.id,
                author_kind="user" if user_id else "visitor",
                author_user_id=user_id,
                body=_clean(body, 5, 4000, "Message"),
                created_at=now,
            )
        )
        await self._s.commit()
        return inquiry

    async def _by_public_id(self, public_id: str) -> Inquiry:
        inquiry = (
            await self._s.execute(select(Inquiry).where(Inquiry.public_id == public_id))
        ).scalar_one_or_none()
        if inquiry is None:
            raise NotFoundError("Inquiry not found.")
        return inquiry

    async def messages(self, inquiry: Inquiry) -> list[InquiryMessage]:
        rows = await self._s.execute(
            select(InquiryMessage).where(InquiryMessage.inquiry_id == inquiry.id).order_by(InquiryMessage.id)
        )
        return list(rows.scalars().all())

    # ----- signed-in user -----

    async def list_mine(self, user_id: int) -> list[Inquiry]:
        rows = await self._s.execute(
            select(Inquiry).where(Inquiry.user_id == user_id).order_by(Inquiry.id.desc()).limit(100)
        )
        return list(rows.scalars().all())

    async def get_mine(self, user_id: int, public_id: str) -> Inquiry:
        inquiry = await self._by_public_id(public_id)
        if inquiry.user_id != user_id:
            raise NotFoundError("Inquiry not found.")  # same answer as a missing one
        return inquiry

    async def reply_as_user(self, user_id: int, public_id: str, body: str) -> InquiryMessage:
        inquiry = await self.get_mine(user_id, public_id)
        now = utcnow()
        message = InquiryMessage(
            inquiry_id=inquiry.id,
            author_kind="user",
            author_user_id=user_id,
            body=_clean(body, 1, 4000, "Message"),
            created_at=now,
        )
        self._s.add(message)
        if inquiry.status is InquiryStatus.CLOSED:
            inquiry.status = InquiryStatus.NEW  # the user reopened it by writing again
        inquiry.updated_at = now
        await self._s.commit()
        return message

    # ----- administrator -----

    async def admin_list(self, status: InquiryStatus | None, limit: int) -> list[Inquiry]:
        stmt = select(Inquiry).order_by(Inquiry.id.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Inquiry.status == status)
        return list((await self._s.execute(stmt)).scalars().all())

    async def admin_get(self, public_id: str) -> Inquiry:
        return await self._by_public_id(public_id)

    async def admin_reply(self, actor: Actor, public_id: str, body: str) -> InquiryMessage:
        inquiry = await self._by_public_id(public_id)
        now = utcnow()
        message = InquiryMessage(
            inquiry_id=inquiry.id,
            author_kind="admin",
            author_user_id=actor.user_id,
            body=_clean(body, 1, 4000, "Reply"),
            created_at=now,
        )
        self._s.add(message)
        inquiry.status = InquiryStatus.IN_PROGRESS
        if inquiry.assigned_admin_id is None:
            inquiry.assigned_admin_id = actor.user_id
        inquiry.updated_at = now
        self._audit.record(
            actor=actor,
            action="inquiry.replied",
            entity_type="inquiry",
            entity_id=inquiry.id,
            new={"status": "in_progress"},
        )
        await self._s.commit()
        return message

    async def admin_set_status(self, actor: Actor, public_id: str, status: InquiryStatus) -> Inquiry:
        if status not in (InquiryStatus.IN_PROGRESS, InquiryStatus.CLOSED):
            raise ValidationDomainError("Status must be in_progress or closed.")
        inquiry = await self._by_public_id(public_id)
        if inquiry.status is status:
            raise InvalidTransitionError("The inquiry is already in that state.")
        inquiry.status = status
        inquiry.updated_at = utcnow()
        self._audit.record(
            actor=actor,
            action="inquiry.status",
            entity_type="inquiry",
            entity_id=inquiry.id,
            new={"status": status.value},
        )
        await self._s.commit()
        return inquiry
