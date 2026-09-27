"""Sliding-window rate limiting.

State is in memory, which is correct for the default single-process
deployment. A multi-instance deployment would swap this class for a shared
store; the interface (``hit`` returning a retry delay) stays the same. The
limitation is documented in README and THREAT-MODEL.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from .errors import RateLimited


class RateLimiter:
    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def hit(self, key: str, limit: int, window: float) -> int | None:
        """Record one attempt. Returns None if allowed, else seconds to wait."""
        now = self._clock()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, math.ceil(bucket[0] + window - now))
            bucket.append(now)
            self._calls += 1
            if self._calls % 1000 == 0:
                self._prune(now, window)
            return None

    def enforce(self, key: str, limit: int, window: float, message: str | None = None) -> None:
        retry = self.hit(key, limit, window)
        if retry is not None:
            raise RateLimited(retry, message)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _prune(self, now: float, window: float) -> None:
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= now - window]
        for key in stale:
            del self._hits[key]
