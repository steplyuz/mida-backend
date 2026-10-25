from datetime import UTC, datetime, timedelta

import pytest

from app.core.domain_errors import AuthenticationError
from app.services.telegram_auth import verify_init_data
from tests.support.telegram import TEST_BOT_TOKEN, make_fields, sign

NOW = datetime(2026, 10, 22, 7, 0, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())


def _verify(init_data: str, *, max_age: int = 300, bot_token: str = TEST_BOT_TOKEN):  # type: ignore[no-untyped-def]
    return verify_init_data(init_data, bot_token=bot_token, max_age_seconds=max_age, now=NOW)


def _fresh(**overrides: object) -> str:
    fields = make_fields(auth_date=NOW_TS, **overrides)  # type: ignore[arg-type]
    return sign(fields)


def test_valid_init_data_yields_identity() -> None:
    identity = _verify(_fresh(telegram_user_id=4242, first_name="  Ali   Valiyev "))
    assert identity.telegram_user_id == 4242
    assert identity.first_name == "Ali Valiyev"
    assert identity.username == "ali_test"
    assert identity.auth_date == NOW_TS


def test_wrong_bot_token_is_rejected() -> None:
    with pytest.raises(AuthenticationError):
        _verify(_fresh(), bot_token="999:OTHER-BOT")


def test_tampered_user_field_breaks_the_signature() -> None:
    init_data = _fresh(telegram_user_id=1001)
    tampered = init_data.replace("1001", "9999")
    with pytest.raises(AuthenticationError, match="signature"):
        _verify(tampered)


def test_missing_hash_is_rejected() -> None:
    init_data = _fresh().split("&hash=")[0]
    with pytest.raises(AuthenticationError):
        _verify(init_data)


@pytest.mark.parametrize("bad_hash", ["ABCDEF", "z" * 64, "A" * 64, ""])
def test_malformed_hash_is_rejected(bad_hash: str) -> None:
    fields = make_fields(auth_date=NOW_TS)
    init_data = "&".join(f"{k}={v}" for k, v in fields.items()) + f"&hash={bad_hash}"
    with pytest.raises(AuthenticationError):
        _verify(init_data)


def test_duplicate_keys_are_rejected() -> None:
    init_data = _fresh() + "&auth_date=" + str(NOW_TS)
    with pytest.raises(AuthenticationError):
        _verify(init_data)


def test_expired_auth_date_is_rejected() -> None:
    stale = sign(make_fields(auth_date=NOW_TS - 301))
    with pytest.raises(AuthenticationError, match="expired"):
        _verify(stale)


def test_auth_date_exactly_at_max_age_is_accepted() -> None:
    edge = sign(make_fields(auth_date=NOW_TS - 300))
    assert _verify(edge).telegram_user_id == 1001


def test_auth_date_far_in_the_future_is_rejected() -> None:
    future = sign(make_fields(auth_date=NOW_TS + 3600))
    with pytest.raises(AuthenticationError):
        _verify(future)


def test_auth_date_small_clock_skew_is_tolerated() -> None:
    skewed = sign(make_fields(auth_date=NOW_TS + 30))
    assert _verify(skewed).telegram_user_id == 1001


def test_non_json_user_is_rejected_even_with_valid_signature() -> None:
    fields = make_fields(auth_date=NOW_TS)
    fields["user"] = "not-json"
    with pytest.raises(AuthenticationError):
        _verify(sign(fields))


@pytest.mark.parametrize("user_id", [0, -5, True, "1001", None])
def test_invalid_user_id_types_are_rejected(user_id: object) -> None:
    import json

    fields = make_fields(auth_date=NOW_TS)
    fields["user"] = json.dumps({"id": user_id, "first_name": "X"})
    with pytest.raises(AuthenticationError):
        _verify(sign(fields))


def test_blank_first_name_is_rejected() -> None:
    import json

    fields = make_fields(auth_date=NOW_TS)
    fields["user"] = json.dumps({"id": 1001, "first_name": "   "})
    with pytest.raises(AuthenticationError):
        _verify(sign(fields))


def test_invalid_username_is_dropped_not_fatal() -> None:
    import json

    fields = make_fields(auth_date=NOW_TS)
    fields["user"] = json.dumps({"id": 1001, "first_name": "Ali", "username": "bad name!"})
    assert _verify(sign(fields)).username is None


@pytest.mark.parametrize("payload", ["", "x" * 5000])
def test_empty_or_oversized_input_is_rejected(payload: str) -> None:
    with pytest.raises(AuthenticationError):
        _verify(payload)


def test_expiry_window_is_relative_to_now() -> None:
    later = NOW + timedelta(seconds=301)
    with pytest.raises(AuthenticationError):
        verify_init_data(_fresh(), bot_token=TEST_BOT_TOKEN, max_age_seconds=300, now=later)
