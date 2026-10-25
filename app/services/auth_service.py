"""Telegram login: verify initData, upsert the user, issue an access token."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.domain_errors import AccountDisabledError, ServiceNotConfiguredError
from app.core.security import issue_access_token
from app.core.time import utcnow
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.telegram_auth import TelegramIdentity, verify_init_data


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: User
    access_token: str
    expires_in: int


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def authenticate(self, init_data: str) -> AuthResult:
        bot_token = self._bot_token()
        identity = verify_init_data(
            init_data,
            bot_token=bot_token,
            max_age_seconds=self._settings.telegram_initdata_max_age_seconds,
            now=utcnow(),
        )
        try:
            user = await self._upsert_user(identity)
            if not user.is_active:
                raise AccountDisabledError()
            token, expires_in = issue_access_token(user.id, self._settings)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return AuthResult(user=user, access_token=token, expires_in=expires_in)

    def _bot_token(self) -> str:
        if self._settings.telegram_bot_token is None:
            raise ServiceNotConfiguredError("Telegram login is not configured.")
        return self._settings.telegram_bot_token.get_secret_value()

    async def _find(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
        return result.scalar_one_or_none()

    async def _upsert_user(self, identity: TelegramIdentity) -> User:
        now = utcnow()
        user = await self._find(identity.telegram_user_id)
        if user is None:
            try:
                is_admin = identity.telegram_user_id in self._settings.admin_telegram_id_set
                user = User(
                    mida_id=await UserRepository(self._session).next_mida_id(),
                    telegram_user_id=identity.telegram_user_id,
                    telegram_username=identity.username,
                    first_name=identity.first_name,
                    last_name=identity.last_name,
                    photo_url=identity.photo_url,
                    role=UserRole.ADMIN if is_admin else UserRole.BUYER,
                    is_active=True,
                    last_seen_at=now,
                )
                self._session.add(user)
                await self._session.flush()
                return user
            except IntegrityError:
                # A concurrent first login created the same Telegram user. Re-read it.
                await self._session.rollback()
                user = await self._find(identity.telegram_user_id)
                if user is None:
                    raise

        if user.mida_id is None:
            user.mida_id = await UserRepository(self._session).next_mida_id()
        if user.role is not UserRole.ADMIN and (
            identity.telegram_user_id in self._settings.admin_telegram_id_set
        ):
            user.role = UserRole.ADMIN
        user.telegram_username = identity.username
        user.first_name = identity.first_name
        user.last_name = identity.last_name
        user.photo_url = identity.photo_url
        user.last_seen_at = now
        await self._session.flush()
        return user
