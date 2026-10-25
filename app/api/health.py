"""System endpoints: /health (process liveness) and /ready (database reachability)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["system"])
logger = logging.getLogger("app.health")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    checks = {"database": "ok"}
    try:
        async with request.app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("readiness check failed", extra={"operation": "ready_check"})
        checks["database"] = "error"

    is_ready = all(state == "ok" for state in checks.values())
    return JSONResponse(
        {"status": "ready" if is_ready else "unavailable", "checks": checks},
        status_code=200 if is_ready else 503,
    )
