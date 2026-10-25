import random

import pytest

from app.services.telegram_group_title import MAX_TITLE_LENGTH, build_group_title
from app.workers.retry_policy import RetryPolicy


class FixedRng(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__(0)
        self._value = value

    def random(self) -> float:
        return self._value


def test_title_has_deal_number_prefix() -> None:
    assert build_group_title("MIDA-000001", "iPhone 15") == "MIDA-000001 | iPhone 15"


def test_whitespace_is_collapsed_and_control_characters_removed() -> None:
    title = build_group_title("MIDA-000002", "  Samsung\t\x00  S24\r\n Ultra  ")
    assert title == "MIDA-000002 | Samsung S24 Ultra"


def test_uzbek_and_cyrillic_text_is_kept() -> None:
    assert build_group_title("MIDA-000003", "Ko‘ylak va ко́фта") == "MIDA-000003 | Ko‘ylak va ко́фта"


def test_long_titles_are_truncated_but_keep_the_deal_number() -> None:
    title = build_group_title("MIDA-000004", "x" * 1000)
    assert len(title) <= MAX_TITLE_LENGTH
    assert title.startswith("MIDA-000004 | ")
    assert title.endswith("…")


def test_empty_product_title_falls_back() -> None:
    assert build_group_title("MIDA-000005", "   \x00  ") == "MIDA-000005 | Bitim"


def test_backoff_doubles_until_the_cap() -> None:
    policy = RetryPolicy(base_seconds=30, max_delay_seconds=3600, jitter_ratio=0.0)
    rng = FixedRng(0.5)
    assert [policy.delay_for(n, rng=rng) for n in (1, 2, 3)] == [30, 60, 120]
    assert policy.delay_for(20, rng=rng) == 3600


def test_jitter_stays_within_the_configured_band() -> None:
    policy = RetryPolicy(base_seconds=100, max_delay_seconds=3600, jitter_ratio=0.2)
    low = policy.delay_for(1, rng=FixedRng(0.0))
    high = policy.delay_for(1, rng=FixedRng(0.999999))
    assert low == 80
    assert high == 120


def test_server_retry_after_overrides_backoff() -> None:
    policy = RetryPolicy()
    assert policy.delay_for(1, rng=FixedRng(0.5), retry_after=45) == 45


def test_attempt_budget_is_enforced() -> None:
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(1) and policy.should_retry(2)
    assert not policy.should_retry(3)


@pytest.mark.parametrize("attempt", [1, 5, 50])
def test_delay_is_always_positive(attempt: int) -> None:
    assert RetryPolicy().delay_for(attempt, rng=random.Random(1)) >= 1
