"""Retry helper for actions that talk to the outside world."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    backoff_seconds: float = 0.5,
    log: Callable[[str], None] | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times with linear backoff; re-raises the last error."""
    attempts = max(int(attempts), 1)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts:
                raise
            if log is not None:
                log(f"attempt {attempt} failed ({type(exc).__name__}: {exc}); retrying")
            if backoff_seconds > 0:
                time.sleep(backoff_seconds * attempt)
    raise AssertionError("unreachable")  # pragma: no cover
