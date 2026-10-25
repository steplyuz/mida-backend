"""Inquiries. The public form needs no login (rate-limited, with a honeypot); signed-in users keep threads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentUserDep, SessionDep, enforce_rate_limit
from app.core.config import Settings
from app.services.inquiry_service import InquiryService

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


class PublicInquiry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    contact: str = Field(min_length=3, max_length=200, description="Phone, email or Telegram username")
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=5, max_length=4000)
    source: str = Field(pattern="^(web_contact|web_partnership)$")
    # Honeypot: real visitors never see or fill this field. Bots usually do.
    website: str | None = Field(default=None, max_length=200)


class UserInquiry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=5, max_length=4000)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


def _thread(inquiry: Any, messages: list[Any]) -> dict[str, Any]:
    return {
        "id": inquiry.public_id,
        "subject": inquiry.subject,
        "status": inquiry.status.value,
        "created_at": inquiry.created_at.isoformat(),
        "updated_at": inquiry.updated_at.isoformat(),
        "messages": [
            {"author_kind": m.author_kind, "body": m.body, "created_at": m.created_at.isoformat()}
            for m in messages
        ],
    }


@router.post("", status_code=201, summary="Send an inquiry from the public site (no login)")
async def send_public(body: PublicInquiry, request: Request, session: SessionDep) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    address = request.client.host if request.client else "unknown"
    enforce_rate_limit(
        request, f"inquiry:{address}", limit=settings.rate_limit_inquiry_per_hour, window_seconds=3600
    )
    if body.website:
        # Looks successful to the bot, stores nothing.
        return {"status": "received"}
    inquiry = await InquiryService(session).create(
        name=body.name, contact=body.contact, subject=body.subject, body=body.message, source=body.source
    )
    return {"status": "received", "id": inquiry.public_id}


@router.post("/mine", status_code=201, summary="Open a support thread (signed-in Mini App user)")
async def open_mine(
    body: UserInquiry, current: CurrentUserDep, session: SessionDep, request: Request
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        f"inquiry-user:{current.user.id}",
        limit=settings.rate_limit_inquiry_per_hour,
        window_seconds=3600,
    )
    user = current.user
    contact = f"@{user.telegram_username}" if user.telegram_username else f"telegram:{user.telegram_user_id}"
    service = InquiryService(session)
    inquiry = await service.create(
        name=user.first_name or "MIDA user",
        contact=contact,
        subject=body.subject,
        body=body.message,
        source="miniapp",
        user_id=user.id,
    )
    return _thread(inquiry, await service.messages(inquiry))


@router.get("/mine", summary="My support threads")
async def list_mine(current: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    service = InquiryService(session)
    rows = await service.list_mine(current.user.id)
    return {
        "data": [
            {
                "id": i.public_id,
                "subject": i.subject,
                "status": i.status.value,
                "updated_at": i.updated_at.isoformat(),
            }
            for i in rows
        ]
    }


@router.get("/mine/{public_id}", summary="One of my support threads")
async def get_mine(public_id: str, current: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    service = InquiryService(session)
    inquiry = await service.get_mine(current.user.id, public_id)
    return _thread(inquiry, await service.messages(inquiry))


@router.post("/mine/{public_id}/messages", status_code=201, summary="Write in my support thread")
async def reply_mine(
    public_id: str, body: ReplyBody, current: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    message = await InquiryService(session).reply_as_user(current.user.id, public_id, body.body)
    return {
        "author_kind": message.author_kind,
        "body": message.body,
        "created_at": message.created_at.isoformat(),
    }
