import time
import unittest

from src.scraper.pace import AccessPacer, is_pressure, request_gap, summarize_failure


class AccessPacerTest(unittest.TestCase):
    def test_healthy_interval_stays_short_until_pressure(self) -> None:
        delays: list[float] = []
        pacer = AccessPacer(sleeper=delays.append, concurrency=3)
        for _ in range(20):
            gap = pacer.interval()
            self.assertGreaterEqual(gap, 0.2)
            self.assertLessEqual(gap, 0.5)
        self.assertEqual(pacer.mark_pressure(), 15.0)
        self.assertEqual(pacer.interval(), 15.0)
        self.assertEqual(pacer.mark_pressure(), 30.0)
        self.assertEqual(pacer.interval(), 30.0)
        self.assertEqual(pacer.mark_pressure(), 60.0)
        self.assertEqual(pacer.mark_pressure(), 60.0)
        self.assertEqual(pacer.interval(), 60.0)
        pacer.mark_success()
        self.assertGreaterEqual(pacer.interval(), 0.2)
        self.assertLessEqual(pacer.interval(), 0.5)
        pacer.sleep(1.5)
        self.assertEqual(delays, [1.5])

    def test_request_gap_keeps_backoff(self) -> None:
        pacer = AccessPacer(sleeper=lambda _delay: None, concurrency=1)
        self.assertLessEqual(request_gap(pacer), 0.5)
        pacer.mark_pressure()
        self.assertEqual(request_gap(pacer), 15.0)

    def test_semaphore_caps_concurrency(self) -> None:
        pacer = AccessPacer(sleeper=lambda _delay: None, concurrency=3)
        for _ in range(3):
            self.assertTrue(pacer.slot().acquire(blocking=False))
        self.assertFalse(pacer.slot().acquire(blocking=False))
        pacer.slot().release()
        self.assertTrue(pacer.slot().acquire(blocking=False))

    def test_limit_fails_fast_when_full_and_releases(self) -> None:
        pacer = AccessPacer(sleeper=lambda _delay: None, concurrency=1)
        with pacer.limit(wait_sec=0.2):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                with pacer.limit(wait_sec=0.2):
                    pass
            self.assertLess(time.monotonic() - started, 1.0)
        with pacer.limit(wait_sec=0.2):
            pass


class FailureSummaryTest(unittest.TestCase):
    def test_reasons_are_short_labels(self) -> None:
        self.assertTrue(is_pressure(429, ""))
        self.assertTrue(is_pressure(503, ""))
        self.assertTrue(is_pressure(None, "timed out"))
        self.assertFalse(is_pressure(200, "failSafeTimeout: 3e3"))
        self.assertFalse(is_pressure(404, "missing"))
        self.assertEqual(summarize_failure("HTTP 429 Too Many Requests"), "429上限")
        self.assertEqual(summarize_failure("TimeoutError: timed out"), "タイムアウト（応答なし）")
        self.assertEqual(summarize_failure("同時接続が 12秒空かなかった"), "タイムアウト（応答なし）")
        self.assertEqual(summarize_failure("接続できません https://example: [Errno 61] Connection refused"), "接続拒否")
        self.assertEqual(summarize_failure("Inertia のデータブロックが見つかりません"), "要素未発見")
        self.assertEqual(summarize_failure("0 件"), "台データなし")
