"""Exponential backoff with bounded jitter. Permanent errors never reach this policy."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 8
    base_seconds: float = 30.0
    max_delay_seconds: float = 3600.0
    jitter_ratio: float = 0.2

    def should_retry(self, attempt: int) -> bool:
        return attempt < self.max_attempts

    def delay_for(self, attempt: int, *, rng: random.Random, retry_after: int | None = None) -> int:
        """attempt is 1-based. A server-provided retry_after wins over the computed backoff."""
        if retry_after is not None and retry_after > 0:
            return int(retry_after)
        doublings = float(2 ** max(0, attempt - 1))
        raw = min(self.max_delay_seconds, self.base_seconds * doublings)
        jitter = raw * self.jitter_ratio * (rng.random() * 2 - 1)
        return max(1, round(raw + jitter))
