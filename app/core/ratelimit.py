"""Rate limiting behind an interface.

SlidingWindowLimiter is per-process and in memory. It is correct for one worker process and for
tests. It is NOT a distributed limiter: with several workers each keeps its own counters, so the
effective limit multiplies. Production needs a shared store (Redis or the database) behind the same
protocol. Do not describe this limiter as production-grade for multi-worker deployments.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from app.core.domain_errors import DomainError

MAX_TRACKED_KEYS = 50_000


class RateLimited(DomainError):
    code = "RATE_LIMITED"
    status_code = 429
    default_message = "Too many requests. Try again later."

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after


class RateLimiter(Protocol):
    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        """Consumes one slot, or raises RateLimited."""


class SlidingWindowLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        now = self._clock()
        with self._lock:
            if len(self._hits) > MAX_TRACKED_KEYS and key not in self._hits:
                self._hits.clear()  # bounded memory: under attack, forget rather than grow forever
            window = self._hits.setdefault(key, deque())
            while window and now - window[0] >= window_seconds:
                window.popleft()
            if len(window) >= limit:
                retry_after = max(1, math.ceil(window_seconds - (now - window[0])))
                raise RateLimited(retry_after)
            window.append(now)
