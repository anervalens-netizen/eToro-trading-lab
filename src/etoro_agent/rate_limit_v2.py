from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RateBudget:
    name: str
    requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if not self.name.strip() or self.requests < 1 or self.window_seconds <= 0:
            raise ValueError("rate budget is invalid")


ETORO_READ = RateBudget("etoro_read", 55, 60.0)
ETORO_WRITE_SHARED = RateBudget("etoro_write_shared", 18, 60.0)
ETORO_COST_PREVIEW = RateBudget("etoro_cost_preview", 18, 60.0)


class RollingWindowLimiter:
    """Thread-safe local guard kept below broker-advertised quotas."""

    def __init__(self, budget: RateBudget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self.clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.budget.window_seconds
        while self._calls and self._calls[0] <= cutoff:
            self._calls.popleft()

    def acquire(self, *, block: bool = False, max_wait_seconds: float = 0.0) -> float:
        started = self.clock()
        while True:
            with self._lock:
                now = self.clock()
                self._prune(now)
                if len(self._calls) < self.budget.requests:
                    self._calls.append(now)
                    return 0.0
                wait = max(0.0, self.budget.window_seconds - (now - self._calls[0]))
            if not block or wait > max_wait_seconds:
                raise TimeoutError(f"local rate budget exhausted: {self.budget.name}")
            time.sleep(wait)
            if self.clock() - started > max_wait_seconds:
                raise TimeoutError(f"local rate budget wait exceeded: {self.budget.name}")

    @property
    def remaining(self) -> int:
        with self._lock:
            self._prune(self.clock())
            return max(0, self.budget.requests - len(self._calls))
