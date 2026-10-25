from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import SessionDep, enforce_rate_limit
from app.core.config import Settings
from app.schemas.auth import TelegramLoginRequest, TokenResponse
from app.schemas.user import UserPublic
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/telegram",
    response_model=TokenResponse,
    summary="Exchange Telegram Mini App initData for an access token",
    responses={401: {"description": "initData signature, format or freshness check failed"}},
)
async def login_with_telegram(
    body: TelegramLoginRequest,
    request: Request,
    session: SessionDep,
) -> TokenResponse:
    settings: Settings = request.app.state.settings
    address = request.client.host if request.client else "unknown"
    enforce_rate_limit(
        request, f"login:{address}", limit=settings.rate_limit_login_per_minute, window_seconds=60
    )
    result = await AuthService(session, settings).authenticate(body.init_data)
    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user=UserPublic.model_validate(result.user),
    )
