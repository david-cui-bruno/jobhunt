from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tailor"))

import tailor  # noqa: E402


class ResumeQualityTests(unittest.TestCase):
    def test_internship_fallback_keeps_returning_student_grad_date(self) -> None:
        result = tailor.apply_grad_date(tailor.BASE_TEX, "Software Engineer Intern")
        self.assertIn("Aug 2024 -- May 2028", result)
        self.assertNotIn("Aug 2024 -- May 2027", result)

    def test_full_time_resume_uses_full_time_grad_date(self) -> None:
        result = tailor.apply_grad_date(tailor.BASE_TEX, "Software Engineer")
        self.assertIn("Aug 2024 -- May 2027", result)

    def test_missing_fill_measurement_is_not_reported_as_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            tailor.subprocess, "run"
        ):
            self.assertIsNone(tailor.measure_fill(Path(tmp) / "resume.pdf"))


if __name__ == "__main__":
    unittest.main()
