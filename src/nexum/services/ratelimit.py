"""In-memory sliding-window limiter for login attempts (per process)."""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, max_attempts: int = 10, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window_seconds:
            hits.popleft()
        return hits

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_attempts

    def retry_after(self, key: str) -> int:
        with self._lock:
            hits = self._prune(key, time.monotonic())
            if len(hits) < self.max_attempts:
                return 0
            return max(int(hits[0] + self.window_seconds - time.monotonic()), 1)

    def hit(self, key: str) -> int:
        """Record a failed attempt; returns how many attempts remain."""
        with self._lock:
            hits = self._prune(key, time.monotonic())
            hits.append(time.monotonic())
            return max(self.max_attempts - len(hits), 0)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = SlidingWindowLimiter(max_attempts=10, window_seconds=15 * 60)
