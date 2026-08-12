from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


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


class SharedRollingWindowLimiter:
    """One fail-closed budget shared by all broker-facing OS processes."""

    def __init__(
        self,
        budget: RateBudget,
        directory: str | Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.clock = clock
        root = Path(directory)
        if not root.is_dir() or root.is_symlink():
            raise RuntimeError("shared eToro rate-limit directory is unavailable")
        self.path = root / f"{budget.name}.json"

    def _locked_calls(self, handle: TextIO, now: float) -> list[float]:
        handle.seek(0)
        raw = handle.read()
        try:
            values = [] if not raw else json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("shared eToro rate-limit state is corrupt") from exc
        if not isinstance(values, list) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ):
            raise RuntimeError("shared eToro rate-limit state is invalid")
        cutoff = now - self.budget.window_seconds
        return [float(value) for value in values if float(value) > cutoff]

    def acquire(self, *, block: bool = False, max_wait_seconds: float = 0.0) -> float:
        started = self.clock()
        while True:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o660)
            with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                now = self.clock()
                calls = self._locked_calls(handle, now)
                if len(calls) < self.budget.requests:
                    calls.append(now)
                    handle.seek(0)
                    json.dump(calls, handle, separators=(",", ":"))
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                    return 0.0
                wait = max(0.0, self.budget.window_seconds - (now - calls[0]))
            if not block or wait > max_wait_seconds:
                raise TimeoutError(f"shared rate budget exhausted: {self.budget.name}")
            time.sleep(wait)
            if self.clock() - started > max_wait_seconds:
                raise TimeoutError(f"shared rate budget wait exceeded: {self.budget.name}")

    @property
    def remaining(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o660)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            calls = self._locked_calls(handle, self.clock())
        return max(0, self.budget.requests - len(calls))
