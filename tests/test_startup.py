import unittest
from datetime import datetime

from weekly_startup import is_report_day


class StartupTests(unittest.TestCase):
    def test_dashboard_opens_only_on_configured_saturday(self):
        self.assertTrue(is_report_day(datetime(2026, 9, 5, 7, 30)))
        self.assertFalse(is_report_day(datetime(2026, 9, 4, 7, 30)))


if __name__ == "__main__":
    unittest.main()
