"""RFC 7807 (Problem Details) responses and global exception handlers."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.domain_errors import DomainError
from app.core.logging import request_id_ctx

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-ID"
PROBLEM_TYPE_PREFIX = "urn:mida:error:"
INTERNAL_ERROR_TYPE = PROBLEM_TYPE_PREFIX + "internal-error"
VALIDATION_ERROR_TYPE = PROBLEM_TYPE_PREFIX + "validation-error"

_LOCATION_KEYS = frozenset({"body", "query", "path", "header", "cookie"})


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def problem_response(
    status_code: int,
    *,
    type_: str,
    detail: str,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": _phrase(status_code),
        "status": status_code,
        "detail": detail,
    }
    if instance is not None:
        body["instance"] = instance
    if errors is not None:
        body["errors"] = errors
    request_id = request_id_ctx.get()
    if request_id is not None:
        body["request_id"] = request_id
        headers = {**(dict(headers) if headers else {}), REQUEST_ID_HEADER: request_id}
    return JSONResponse(
        body,
        status_code=status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=headers,
    )


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else _phrase(exc.status_code)
    return problem_response(
        exc.status_code,
        type_=f"{PROBLEM_TYPE_PREFIX}http-{exc.status_code}",
        detail=detail,
        instance=request.url.path,
        headers=exc.headers,
    )


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors: list[dict[str, Any]] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"] if part not in _LOCATION_KEYS)
        # The submitted value is intentionally not echoed back.
        errors.append({"field": field, "code": err["type"], "message": err["msg"]})
    return problem_response(
        422,
        type_=VALIDATION_ERROR_TYPE,
        detail="Request validation failed.",
        instance=request.url.path,
        errors=errors,
    )


async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    headers: dict[str, str] | None = None
    if exc.status_code == 401:
        headers = {"WWW-Authenticate": "Bearer"}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers = {"Retry-After": str(retry_after)}
    return problem_response(
        exc.status_code,
        type_=f"{PROBLEM_TYPE_PREFIX}{exc.code.lower().replace('_', '-')}",
        detail=exc.message,
        instance=request.url.path,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
