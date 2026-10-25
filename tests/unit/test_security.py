from datetime import timedelta

import jwt
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.domain_errors import ServiceNotConfiguredError
from app.core.security import AUDIENCE, ISSUER, decode_access_token, issue_access_token
from app.core.time import utcnow

SECRET = "s" * 32


def _settings(secret: str | None = SECRET, ttl: int = 900) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_secret_key=SecretStr(secret) if secret else None,
        access_token_ttl_seconds=ttl,
    )


def test_round_trip_returns_user_id() -> None:
    settings = _settings()
    token, ttl = issue_access_token(42, settings)
    assert ttl == 900
    assert decode_access_token(token, settings) == 42


def test_expired_token_is_rejected() -> None:
    settings = _settings(ttl=60)
    token, _ = issue_access_token(42, settings, now=utcnow() - timedelta(hours=2))
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token, settings)


def test_token_signed_with_other_secret_is_rejected() -> None:
    token, _ = issue_access_token(42, _settings("a" * 32))
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token, _settings("b" * 32))


def test_unsigned_none_algorithm_token_is_rejected() -> None:
    payload = {
        "sub": "42",
        "iat": 1,
        "exp": 9999999999,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": "access",
    }
    forged = jwt.encode(payload, None, algorithm="none")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(forged, _settings())


def test_token_for_another_audience_is_rejected() -> None:
    payload = {
        "sub": "42",
        "iat": 1,
        "exp": 9999999999,
        "iss": ISSUER,
        "aud": "other-app",
        "typ": "access",
    }
    forged = jwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(forged, _settings())


def test_token_of_wrong_type_is_rejected() -> None:
    payload = {
        "sub": "42",
        "iat": 1,
        "exp": 9999999999,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": "refresh",
    }
    forged = jwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(forged, _settings())


def test_token_without_expiry_is_rejected() -> None:
    payload = {"sub": "42", "iat": 1, "iss": ISSUER, "aud": AUDIENCE, "typ": "access"}
    forged = jwt.encode(payload, SECRET, algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(forged, _settings())


def test_issuing_without_a_configured_secret_fails_closed() -> None:
    with pytest.raises(ServiceNotConfiguredError):
        issue_access_token(42, _settings(secret=None))
