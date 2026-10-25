"""Access tokens: HS256 JWT with a single issuer and verifier, so one 32+ byte secret is enough.

Tokens carry only the user id. Role and active status are read from the database on every
request, so a demotion or deactivation takes effect immediately, not at token expiry.
"""

from __future__ import annotations

from datetime import datetime

import jwt

from app.core.config import Settings
from app.core.domain_errors import ServiceNotConfiguredError
from app.core.time import utcnow

ISSUER = "mida-api"
AUDIENCE = "mida-mini-app"
ALGORITHM = "HS256"
TOKEN_TYPE = "access"  # noqa: S105  (token type label, not a secret)
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "aud", "iss"]


def _signing_key(settings: Settings) -> str:
    if settings.app_secret_key is None:
        raise ServiceNotConfiguredError("Authentication is not configured.")
    return settings.app_secret_key.get_secret_value()


def issue_access_token(user_id: int, settings: Settings, *, now: datetime | None = None) -> tuple[str, int]:
    issued_at = int((now or utcnow()).timestamp())
    ttl = settings.access_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": TOKEN_TYPE,
    }
    return jwt.encode(payload, _signing_key(settings), algorithm=ALGORITHM), ttl


def decode_access_token(token: str, settings: Settings) -> int:
    """Returns the user id. Raises jwt.PyJWTError on any signature, claim or type problem."""
    payload = jwt.decode(
        token,
        _signing_key(settings),
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": _REQUIRED_CLAIMS},
    )
    if payload.get("typ") != TOKEN_TYPE:
        raise jwt.InvalidTokenError("unexpected token type")
    return int(payload["sub"])
