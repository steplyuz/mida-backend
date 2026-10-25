"""Shared FastAPI dependencies: DB session, authenticated user, admin guard and services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor
from app.core.config import Settings
from app.core.domain_errors import AccountDisabledError, AuthenticationError, PermissionDeniedError
from app.core.security import decode_access_token
from app.models.enums import UserRole
from app.models.user import User
from app.services.deal_service import DealService
from app.services.pricing_service import PricingService

_bearer = HTTPBearer(auto_error=False)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit explicitly."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]


def enforce_rate_limit(request: Request, key: str, *, limit: int, window_seconds: int) -> None:
    limiter = request.app.state.rate_limiter
    limiter.check(key, limit=limit, window_seconds=window_seconds)


IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    actor: Actor


async def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if credentials is None:
        raise AuthenticationError("Authentication required.")
    settings: Settings = request.app.state.settings
    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except (jwt.PyJWTError, ValueError):
        raise AuthenticationError("Invalid or expired token.") from None

    # Re-read on every request: deactivation and role changes apply immediately.
    user = await session.get(User, user_id)
    if user is None:
        raise AuthenticationError("Invalid or expired token.")
    if not user.is_active:
        raise AccountDisabledError()

    actor = Actor.admin(user.id) if user.role is UserRole.ADMIN else Actor.user(user.id)
    return CurrentUser(user=user, actor=actor)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def require_admin(current: CurrentUserDep) -> CurrentUser:
    if current.user.role is not UserRole.ADMIN:
        raise PermissionDeniedError("Administrator access required.")
    return current


AdminUserDep = Annotated[CurrentUser, Depends(require_admin)]


def get_deal_service(request: Request, session: SessionDep) -> DealService:
    settings: Settings = request.app.state.settings
    return DealService(session, PricingService.from_settings(settings))


DealServiceDep = Annotated[DealService, Depends(get_deal_service)]
