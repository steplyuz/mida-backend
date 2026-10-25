"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import REQUEST_ID_HEADER, register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.integrations.payments.factory import build_payment_provider
from app.lifespan import lifespan


def create_app(
    settings: Settings | None = None,
    *,
    telegram_gateway: object | None = None,
    payment_provider: object | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    setup_logging(resolved.log_level)
    docs_enabled = resolved.app_env != "production"

    app = FastAPI(
        title=resolved.app_name,
        debug=resolved.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = resolved
    app.state.telegram_gateway = telegram_gateway
    app.state.rate_limiter = SlidingWindowLimiter()
    # Fails fast at startup if the mock is enabled in production or misconfigured.
    app.state.payment_provider = payment_provider or build_payment_provider(resolved)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    # Added last, so it is the outermost middleware and covers every response.
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix=resolved.api_v1_prefix)
    return app


app = create_app()
