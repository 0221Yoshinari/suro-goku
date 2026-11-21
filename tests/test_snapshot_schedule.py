import time
import unittest
import unittest.mock

from src.dashboard import prefecture_snapshot as module


class SnapshotScheduleTest(unittest.TestCase):
    def tearDown(self) -> None:
        with module._LOCK:
            module._DIRTY.clear()

    def test_debounce_extends_but_does_not_shrink(self) -> None:
        with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
            module.schedule("fukuoka", delay=5.0)
        with module._LOCK:
            first = module._DIRTY["fukuoka"]
        time.sleep(0.05)
        with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
            module.schedule("fukuoka", delay=5.0)
        with module._LOCK:
            second = module._DIRTY["fukuoka"]
        self.assertGreaterEqual(second, first)

    def test_immediate_request_moves_due_earlier(self) -> None:
        with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
            module.schedule("fukuoka", delay=5.0)
        with module._LOCK:
            first = module._DIRTY["fukuoka"]
        with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
            module.schedule("fukuoka", delay=0)
        with module._LOCK:
            second = module._DIRTY["fukuoka"]
        self.assertLessEqual(second, first)


if __name__ == "__main__":
    unittest.main()