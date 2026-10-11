import time
import unittest

from src.scraper.deadline import abandoned_threads
from src.scraper.deadline import run_within
from src.scheduler import cycle_should_abort


class HallTimeoutTest(unittest.TestCase):
    def test_returns_value_when_fast(self) -> None:
        self.assertEqual(run_within(lambda: 7, timeout=2, label="速い店舗"), 7)

    def test_timeout_does_not_block_the_caller(self) -> None:
        started = time.monotonic()

        def slow() -> None:
            time.sleep(5)

        with self.assertRaises(TimeoutError):
            run_within(slow, timeout=0.4, label="遅い店舗")
        self.assertLess(time.monotonic() - started, 2)

    def test_timeout_counts_abandoned_work(self) -> None:
        before = abandoned_threads()
        with self.assertRaises(TimeoutError):
            run_within(lambda: time.sleep(2), timeout=0.2, label="遅い店舗")
        self.assertEqual(abandoned_threads(), before + 1)


class CycleGuardTest(unittest.TestCase):
    def test_abort_when_idle_or_over_budget(self) -> None:
        self.assertEqual(
            cycle_should_abort(elapsed_sec=10, idle_sec=100, budget_sec=1000, stall_limit=60),
            "idle",
        )
        self.assertEqual(
            cycle_should_abort(elapsed_sec=1000, idle_sec=1, budget_sec=900, stall_limit=60),
            "budget",
        )
        self.assertIsNone(
            cycle_should_abort(elapsed_sec=10, idle_sec=1, budget_sec=900, stall_limit=60)
        )
