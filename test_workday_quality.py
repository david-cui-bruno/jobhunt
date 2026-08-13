from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import workday  # noqa: E402


class WorkdayQualityTests(unittest.TestCase):
    def test_day_precision_requires_an_explicit_full_date(self) -> None:
        self.assertIsNone(workday._workday_date_parts("2005", has_day=True))
        self.assertIsNone(workday._workday_date_parts("May 2005", has_day=True))
        self.assertEqual(
            workday._workday_date_parts("05/17/2005", has_day=True),
            ("05", "17", "2005"),
        )

    def test_month_year_accepts_grounded_month_or_year(self) -> None:
        self.assertEqual(
            workday._workday_date_parts("09/2024", has_day=False),
            ("09", "", "2024"),
        )
        self.assertEqual(
            workday._workday_date_parts("graduating 2028", has_day=False),
            ("05", "", "2028"),
        )


if __name__ == "__main__":
    unittest.main()
