"""Application lifespan: owns the DB engine, the Telegram gateway and the optional outbox worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.core.config import Settings
from app.db.session import create_engine_from_url, create_session_factory
from app.integrations.telegram.bot_api import BotApiClient, BotClient, UnconfiguredBot
from app.integrations.telegram.factory import build_gateway
from app.workers.outbox_worker import OutboxWorker

logger = logging.getLogger("app.lifespan")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    engine = create_engine_from_url(settings.database_url)
    session_factory = create_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory

    gateway: Any = app.state.telegram_gateway or build_gateway(settings)
    app.state.telegram_gateway = gateway

    bot: BotClient = (
        BotApiClient(settings.telegram_bot_token.get_secret_value())
        if settings.telegram_bot_token is not None and settings.bot_configured
        else UnconfiguredBot()
    )
    app.state.bot = bot

    # The group bot is a separate bot with its own token. Tests may inject a fake beforehand.
    group_bot: BotClient = getattr(app.state, "group_bot", None) or (
        BotApiClient(settings.group_bot_token.get_secret_value())
        if settings.group_bot_token is not None and settings.group_bot_token.get_secret_value()
        else UnconfiguredBot()
    )
    app.state.group_bot = group_bot

    stop = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None
    if settings.worker_active:
        worker = OutboxWorker(
            session_factory,
            gateway,
            logo_path=settings.logo_path,
            invite_ttl_seconds=settings.telegram_invite_ttl_seconds,
            policy=None,
            bot=bot,
            group_bot=group_bot,
            miniapp_url=settings.telegram_miniapp_url,
        )
        worker_task = asyncio.create_task(worker.run_forever(settings.telegram_worker_interval_seconds, stop))

    # Never log the database URL or any credential; log only the dialect and feature flags.
    logger.info(
        "application started",
        extra={
            "operation": "startup",
            "app_env": settings.app_env,
            "dialect": engine.dialect.name,
            "telegram_configured": settings.mtproto_configured,
            "worker_enabled": settings.worker_active,
        },
    )
    try:
        yield
    finally:
        stop.set()
        if worker_task is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(worker_task, timeout=10)
        await gateway.close()
        await bot.close()
        await group_bot.close()
        await engine.dispose()
        logger.info("application stopped", extra={"operation": "shutdown"})
