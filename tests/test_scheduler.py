"""轮询调度的安全边界回归测试。"""
import unittest
from unittest.mock import patch

from src.scheduler import AdaptiveScheduler, ScheduleConfig, MIN_INTERVAL_SECONDS


class SchedulerMinimumIntervalTests(unittest.TestCase):
    def test_jitter_cannot_reduce_minimum_interval(self):
        scheduler = AdaptiveScheduler(
            ScheduleConfig(adaptive=False, interval=MIN_INTERVAL_SECONDS, jitter=180)
        )
        with patch("src.scheduler.random.randint", return_value=-100) as randint:
            delay, _ = scheduler.next_delay()

        self.assertEqual(delay, MIN_INTERVAL_SECONDS)
        randint.assert_not_called()

    def test_jitter_is_limited_by_headroom_above_minimum(self):
        scheduler = AdaptiveScheduler(
            ScheduleConfig(adaptive=False, interval=450, jitter=180)
        )
        with patch("src.scheduler.random.randint", return_value=-150):
            delay, _ = scheduler.next_delay()

        self.assertEqual(delay, MIN_INTERVAL_SECONDS)

    def test_normal_jitter_is_preserved_when_safe(self):
        scheduler = AdaptiveScheduler(
            ScheduleConfig(adaptive=False, interval=600, jitter=180)
        )
        with patch("src.scheduler.random.randint", return_value=-180):
            delay, _ = scheduler.next_delay()

        self.assertEqual(delay, 420)

    def test_runtime_guard_covers_unvalidated_schedule_config(self):
        scheduler = AdaptiveScheduler(
            ScheduleConfig(adaptive=False, interval=120, jitter=0)
        )
        delay, _ = scheduler.next_delay()

        self.assertEqual(delay, MIN_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
