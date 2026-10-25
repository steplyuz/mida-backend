import pytest

from app.core.domain_errors import IdempotencyKeyError, ValidationDomainError
from app.core.pagination import decode_cursor, encode_cursor
from app.services.idempotency_service import build_request_hash, validate_key


def test_cursor_round_trip() -> None:
    payload = {"s": "-created_at", "t": "2026-10-22T07:00:00+00:00", "i": 42}
    assert decode_cursor(encode_cursor(payload)) == payload


@pytest.mark.parametrize("token", ["", "!!!", "bm90LWEtZGljdA", "x" * 600])
def test_malformed_cursors_are_rejected(token: str) -> None:
    with pytest.raises(ValidationDomainError):
        decode_cursor(token)


@pytest.mark.parametrize("key", [None, "", "short", "has space here", "bad/slash/key!", "a" * 129])
def test_invalid_idempotency_keys_are_rejected(key: str | None) -> None:
    with pytest.raises(IdempotencyKeyError):
        validate_key(key)


def test_valid_idempotency_key_is_returned_unchanged() -> None:
    assert validate_key("deal-create:2026-10-22.001") == "deal-create:2026-10-22.001"


def test_request_hash_is_stable_and_order_independent() -> None:
    a = build_request_hash("deal.create", {"title": "X", "amount": 5})
    b = build_request_hash("deal.create", {"amount": 5, "title": "X"})
    assert a == b


def test_request_hash_differs_by_operation_and_payload() -> None:
    base = build_request_hash("deal.create", {"amount": 5})
    assert base != build_request_hash("deal.cancel", {"amount": 5})
    assert base != build_request_hash("deal.create", {"amount": 6})
