"""Web admin login with the Telegram Login Widget. Only existing administrator accounts can sign in."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from sqlalchemy import select

from app.api.deps import SessionDep, enforce_rate_limit
from app.core.config import Settings
from app.core.domain_errors import AccountDisabledError, PermissionDeniedError, ServiceNotConfiguredError
from app.core.security import issue_access_token
from app.core.time import utcnow
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import TokenResponse
from app.schemas.user import UserPublic
from app.services.telegram_widget import verify_widget_login

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/telegram-widget", response_model=TokenResponse, summary="Administrator sign-in (web panel)")
async def login_with_widget(
    request: Request, session: SessionDep, data: Annotated[dict[str, Any], Body()]
) -> TokenResponse:
    settings: Settings = request.app.state.settings
    address = request.client.host if request.client else "unknown"
    enforce_rate_limit(
        request,
        f"admin-login:{address}",
        limit=settings.rate_limit_login_per_minute,
        window_seconds=60,
    )
    if settings.telegram_bot_token is None:
        raise ServiceNotConfiguredError("Telegram login is not configured.")

    identity = verify_widget_login(
        data,
        bot_token=settings.telegram_bot_token.get_secret_value(),
        max_age_seconds=settings.telegram_initdata_max_age_seconds,
        now=utcnow(),
    )
    user = (
        await session.execute(select(User).where(User.telegram_user_id == identity.telegram_user_id))
    ).scalar_one_or_none()
    if user is None or user.role is not UserRole.ADMIN:
        # Same refusal for "not registered" and "not an administrator": nothing is revealed.
        raise PermissionDeniedError("Administrator access required.")
    if not user.is_active:
        raise AccountDisabledError()
    user.last_seen_at = utcnow()
    await session.commit()
    token, expires_in = issue_access_token(user.id, settings)
    return TokenResponse(access_token=token, expires_in=expires_in, user=UserPublic.model_validate(user))
