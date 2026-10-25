"""Processes due outbox events. Safe to run in several processes: each event is claimed by lease.

Outcome rules:
  done      -> event completed
  retryable -> rescheduled with backoff (or server-given retry_after), until max attempts
  permanent -> event failed (dead-letter); an admin may retry it after fixing the cause
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.time import utcnow
from app.integrations.telegram.bot_api import BotClient, UnconfiguredBot
from app.integrations.telegram.errors import PermanentTelegramError, TelegramGatewayError
from app.integrations.telegram.gateway import TelegramGroupGateway
from app.services.notification_service import NotificationSender, send_group_message
from app.services.outbox_service import (
    AVATAR_EVENT,
    GROUP_EVENT,
    GROUP_MESSAGE_EVENT,
    NOTIFY_EVENT,
    ClaimedEvent,
    OutboxService,
)
from app.services.telegram_group_service import TelegramGroupService
from app.workers.retry_policy import RetryPolicy

logger = logging.getLogger("app.worker")
Factory = async_sessionmaker[AsyncSession]


class OutboxWorker:
    def __init__(
        self,
        session_factory: Factory,
        gateway: TelegramGroupGateway,
        *,
        logo_path: Path,
        invite_ttl_seconds: int,
        policy: RetryPolicy | None = None,
        bot: BotClient | None = None,
        group_bot: BotClient | None = None,
        miniapp_url: str | None = None,
        lease_seconds: int = 120,
        batch_size: int = 10,
        rng: random.Random | None = None,
    ) -> None:
        self._sf = session_factory
        self._policy = policy or RetryPolicy()
        self._lease = lease_seconds
        self._batch = batch_size
        # Jitter only spreads retries; it is not security-sensitive.
        self._rng = rng or random.Random()  # noqa: S311
        self._notifier = NotificationSender(
            session_factory, bot or UnconfiguredBot(), miniapp_url=miniapp_url
        )
        self._group_bot: BotClient = group_bot or UnconfiguredBot()
        self._sf_group = session_factory
        self._group = TelegramGroupService(
            session_factory, gateway, logo_path=logo_path, invite_ttl_seconds=invite_ttl_seconds
        )

    async def run_once(self, now: datetime | None = None) -> int:
        moment = now or utcnow()
        async with self._sf() as session:
            events = await OutboxService(session).claim_due(
                now=moment, lease_seconds=self._lease, limit=self._batch
            )
        for event in events:
            await self._process(event, moment)
        return len(events)

    async def run_forever(self, interval_seconds: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("outbox iteration failed", extra={"operation": "worker_loop"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)

    async def _process(self, event: ClaimedEvent, now: datetime) -> None:
        log = {"operation": event.event_type, "event_id": event.id, "attempt": event.attempt_count}
        try:
            await self._dispatch(event)
        except PermanentTelegramError as exc:
            logger.warning(
                "outbox event failed permanently",
                extra={**log, "result": "failed", "error_code": exc.code},
            )
            await self._give_up(event, exc.code, now)
            return
        except TelegramGatewayError as exc:
            logger.warning(
                "outbox event hit a telegram error",
                extra={
                    **log,
                    "result": "retry" if exc.retryable else "failed",
                    "error_code": exc.category.value,
                },
            )
            await self._retry_or_give_up(
                event, exc.category.value, now, retryable=exc.retryable, retry_after=exc.retry_after
            )
            return
        except Exception:
            logger.exception(
                "outbox event crashed",
                extra={**log, "result": "retry", "error_code": "internal_error"},
            )
            await self._retry_or_give_up(event, "internal_error", now, retryable=True)
            return

        async with self._sf() as session:
            await OutboxService(session).complete(event, now=utcnow())
        logger.info("outbox event done", extra={**log, "result": "done"})

    async def _dispatch(self, event: ClaimedEvent) -> None:
        if event.event_type == NOTIFY_EVENT:
            await self._notifier.send(event.payload)
            return
        if event.event_type == GROUP_MESSAGE_EVENT:
            await send_group_message(self._sf_group, self._group_bot, event.payload)
            return
        deal_id = event.payload.get("deal_id")
        if not isinstance(deal_id, int):
            raise PermanentTelegramError("bad_payload")
        if event.event_type == GROUP_EVENT:
            await self._group.create_group(deal_id)
        elif event.event_type == AVATAR_EVENT:
            await self._group.set_avatar(deal_id)
        else:
            raise PermanentTelegramError("unknown_event_type")

    async def _retry_or_give_up(
        self,
        event: ClaimedEvent,
        code: str,
        now: datetime,
        *,
        retryable: bool,
        retry_after: int | None = None,
    ) -> None:
        if retryable and self._policy.should_retry(event.attempt_count):
            delay = self._policy.delay_for(event.attempt_count, rng=self._rng, retry_after=retry_after)
            async with self._sf() as session:
                await OutboxService(session).reschedule(
                    event, next_attempt_at=now + timedelta(seconds=delay), error_code=code
                )
            return
        await self._give_up(event, code, now)

    async def _give_up(self, event: ClaimedEvent, code: str, now: datetime) -> None:
        async with self._sf() as session:
            await OutboxService(session).fail(event, error_code=code, now=utcnow())
        if event.event_type == GROUP_EVENT:
            deal_id = event.payload.get("deal_id")
            if isinstance(deal_id, int):
                await self._group.mark_group_failed(deal_id, code)
