"""Request context middleware: correlation ID, access log, and safe 500 responses."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import INTERNAL_ERROR_TYPE, REQUEST_ID_HEADER, problem_response
from app.core.logging import request_id_ctx

logger = logging.getLogger("app.request")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    """Pure ASGI middleware. Never leaks tracebacks or exception text to clients."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False
        method = scope["method"]
        path = scope["path"]

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "unhandled exception",
                extra={"operation": "http", "method": method, "path": path},
            )
            if response_started:
                raise
            status_code = 500
            response = problem_response(
                500,
                type_=INTERNAL_ERROR_TYPE,
                detail="An unexpected error occurred. Please try again later.",
                instance=path,
            )
            await response(scope, receive, send)
        finally:
            logger.info(
                "request completed",
                extra={
                    "operation": "http",
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_ctx.reset(token)

    @staticmethod
    def _incoming_request_id(scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                candidate = value.decode("latin-1")
                return candidate if _VALID_REQUEST_ID.match(candidate) else None
        return None
