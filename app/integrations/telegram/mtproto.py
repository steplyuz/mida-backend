"""MTProto adapter built on Telethon, using the MIDA admin user session.

STATUS: written against the Telethon 1.x API and import-checked, but NOT yet verified against live
Telegram: no credentials were available when this was written. The live test (telegram_live marker)
exercises it once credentials exist. Until then treat it as unverified.

Errors are mapped to ErrorCategory so the worker can decide whether to retry. Raw Telethon messages
are not forwarded, because they can contain phone numbers or links.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    UserAlreadyParticipantError,
    UserNotMutualContactError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditPhotoRequest,
    InviteToChannelRequest,
)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import InputChatUploadedPhoto

from app.integrations.telegram.errors import ErrorCategory, TelegramGatewayError

logger = logging.getLogger("app.telegram")


class TelethonGateway:
    def __init__(self, *, api_id: int, api_hash: str, session_path: str) -> None:
        self._client = TelegramClient(session_path, api_id, api_hash)

    async def _connected(self) -> TelegramClient:
        """Connects and verifies the session. The failure reason is kept apart:

        - session really is not logged in -> AUTHENTICATION (permanent, needs the one-time setup)
        - flood wait / network trouble    -> RATE_LIMIT / TEMPORARY_NETWORK (the worker retries)
        - anything else                   -> AUTHENTICATION, with the exception CLASS logged.
        Telethon's is_user_authorized() hides all of these behind a plain False, so it is not used.
        """
        try:
            if not self._client.is_connected():
                await self._client.connect()
            me = await self._client.get_me()
        except FloodWaitError as exc:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=exc.seconds) from None
        except (ConnectionError, OSError, TimeoutError):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None
        except Exception as exc:
            logger.warning(
                "telegram session check failed",
                extra={"operation": "session_check", "error_type": type(exc).__name__},
            )
            raise TelegramGatewayError(
                ErrorCategory.AUTHENTICATION,
                "The MTProto admin session could not be verified. See error_type in the log.",
            ) from None
        if me is None:
            logger.warning(
                "telegram session is not logged in",
                extra={"operation": "session_check", "error_type": "not_authorized"},
            )
            raise TelegramGatewayError(
                ErrorCategory.AUTHENTICATION,
                "The MTProto admin session is not authorized. Run the one-time setup.",
            )
        return self._client

    async def create_supergroup(self, title: str) -> int:
        client = await self._connected()
        try:
            result: Any = await client(CreateChannelRequest(title=title, about="", megagroup=True))
        except FloodWaitError as exc:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=exc.seconds) from None
        except (ConnectionError, OSError):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None
        except Exception as exc:
            # Only the exception CLASS is logged (e.g. UserDeactivatedBanError): the message can
            # contain phone numbers, so it is never forwarded.
            logger.warning(
                "telegram rejected group creation",
                extra={"operation": "group_create", "error_type": type(exc).__name__},
            )
            raise TelegramGatewayError(ErrorCategory.GROUP_CREATION) from None
        channel = result.chats[0]
        # Bot-API style id for supergroups: -100 followed by the internal channel id.
        return int(f"-100{channel.id}")

    async def add_member(self, chat_id: int, telegram_user_id: int) -> None:
        client = await self._connected()
        try:
            channel = await client.get_input_entity(chat_id)
            user = await client.get_input_entity(telegram_user_id)
            await client(InviteToChannelRequest(channel=channel, users=[user]))
        except UserAlreadyParticipantError:
            return
        except FloodWaitError as exc:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=exc.seconds) from None
        except (UserPrivacyRestrictedError, UserNotMutualContactError, ValueError):
            raise TelegramGatewayError(ErrorCategory.INVALID_PARTICIPANT) from None
        except ChatAdminRequiredError:
            raise TelegramGatewayError(ErrorCategory.PERMISSION) from None
        except (ConnectionError, OSError):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None

    async def create_invite_link(self, chat_id: int, *, usage_limit: int, expire_seconds: int) -> str:
        from datetime import UTC, datetime, timedelta

        client = await self._connected()
        try:
            channel = await client.get_input_entity(chat_id)
            expires = datetime.now(UTC) + timedelta(seconds=expire_seconds)
            result: Any = await client(
                ExportChatInviteRequest(peer=channel, expire_date=expires, usage_limit=usage_limit)
            )
        except FloodWaitError as exc:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=exc.seconds) from None
        except ChatAdminRequiredError:
            raise TelegramGatewayError(ErrorCategory.PERMISSION) from None
        except (ConnectionError, OSError):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None
        link: str = result.link
        return link

    async def set_photo(self, chat_id: int, path: str) -> None:
        client = await self._connected()
        try:
            channel = await client.get_input_entity(chat_id)
            uploaded = await client.upload_file(Path(path))
            await client(EditPhotoRequest(channel=channel, photo=InputChatUploadedPhoto(file=uploaded)))
        except FloodWaitError as exc:
            raise TelegramGatewayError(ErrorCategory.RATE_LIMIT, retry_after=exc.seconds) from None
        except (ConnectionError, OSError):
            raise TelegramGatewayError(ErrorCategory.TEMPORARY_NETWORK) from None
        except Exception:
            raise TelegramGatewayError(ErrorCategory.AVATAR) from None

    async def close(self) -> None:
        if self._client.is_connected():
            await self._client.disconnect()
