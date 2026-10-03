import inspect
import time
import unittest

from fourfunds.runtime import BotRunner


class RuntimeTests(unittest.TestCase):
    def test_default_clock_reports_current_epoch_milliseconds(self):
        default_clock = inspect.signature(BotRunner.__init__).parameters[
            "clock_ms"
        ].default

        before_ms = time.time_ns() // 1_000_000
        reading_ms = default_clock()
        after_ms = time.time_ns() // 1_000_000

        self.assertGreaterEqual(reading_ms, before_ms)
        self.assertLessEqual(reading_ms, after_ms)
