from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from etoro_agent.rate_limit_v2 import (
    RateBudget,
    RollingWindowLimiter,
    SharedRollingWindowLimiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class RateLimitV2Tests(unittest.TestCase):
    def test_rolling_window_is_deterministic_and_conservative(self) -> None:
        clock = FakeClock()
        limiter = RollingWindowLimiter(RateBudget("test", 2, 60), clock=clock)
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(limiter.remaining, 0)
        with self.assertRaises(TimeoutError):
            limiter.acquire()
        clock.value = 61
        self.assertEqual(limiter.remaining, 2)
        limiter.acquire()
        self.assertEqual(limiter.remaining, 1)

    def test_shared_budget_is_one_authority_across_clients(self) -> None:
        clock = FakeClock()
        with TemporaryDirectory() as folder:
            first = SharedRollingWindowLimiter(RateBudget("shared", 2, 60), folder, clock=clock)
            second = SharedRollingWindowLimiter(RateBudget("shared", 2, 60), folder, clock=clock)
            first.acquire()
            second.acquire()
            self.assertEqual(first.remaining, 0)
            with self.assertRaisesRegex(TimeoutError, "shared"):
                first.acquire()
            clock.value = 61
            self.assertEqual(second.remaining, 2)


if __name__ == "__main__":
    unittest.main()
