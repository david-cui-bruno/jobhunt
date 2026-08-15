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
    def test_internship_resume_uses_truthful_grad_date(self) -> None:
        result = tailor.apply_grad_date(tailor.BASE_TEX, "Software Engineer Intern")
        self.assertIn("Aug 2024 -- June 2028", result)
        self.assertNotIn("Aug 2024 -- May 2027", result)

    def test_full_time_resume_does_not_change_education_fact(self) -> None:
        result = tailor.apply_grad_date(tailor.BASE_TEX, "Software Engineer")
        self.assertIn("Aug 2024 -- June 2028", result)
        self.assertNotIn("Aug 2024 -- May 2027", result)

    def test_missing_fill_measurement_is_not_reported_as_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            tailor.subprocess, "run"
        ):
            self.assertIsNone(tailor.measure_fill(Path(tmp) / "resume.pdf"))

    def test_grounded_resume_keeps_identity_and_project_facts_immutable(self) -> None:
        result = tailor.build_grounded_resume(
            "AI Engineer Intern - Innovation Team",
            "London AI internship using Python, PyTorch, and machine learning",
        )

        self.assertNotIn("U.S. Citizen", result)
        self.assertNotIn("relocat", result.lower())
        self.assertNotIn("visa", result.lower())
        self.assertNotIn("BUNAC", result)
        self.assertNotIn("expense-splitting", result)
        self.assertNotIn("Pydantic AI", result)
        self.assertNotIn("LlamaIndex", result)
        self.assertIn("{Co-Founder \\& CTO}{Mar 2026 -- Jul 2026}", result)
        self.assertIn("{Software Engineering Intern}{Aug 2025 -- Dec 2025}", result)
        self.assertIn("multi-host storage plans", result)

        base_header = tailor.BASE_TEX.split("%----------HEADING-----------------", 1)[1].split(
            "%-----------EDUCATION-----------------", 1
        )[0]
        result_header = result.split("%----------HEADING-----------------", 1)[1].split(
            "%-----------EDUCATION-----------------", 1
        )[0]
        self.assertEqual(base_header, result_header)

    def test_ai_resume_has_eight_verified_relevant_courses(self) -> None:
        result = tailor.build_grounded_resume(
            "AI Engineer Intern",
            "Machine learning, deep learning, PyTorch, and computer vision",
        )
        match = __import__("re").search(
            r"\\resumeItem\{Coursework\}\s*\{([^}]+)\}", result,
        )
        self.assertIsNotNone(match)
        courses = [course.strip() for course in match.group(1).split(",")]
        self.assertEqual(8, len(courses))
        self.assertEqual("Machine Learning", courses[0])
        self.assertIn("Deep Learning", courses)
        self.assertIn("Operating Systems", courses[-1])

    def test_security_boilerplate_does_not_override_ml_title(self) -> None:
        role = tailor.infer_role_type(
            "Machine Learning Engineer Intern",
            "Build model infrastructure following security best practices.",
        )
        self.assertEqual("ml", role)

    def test_security_clearance_does_not_override_backend_title(self) -> None:
        role = tailor.infer_role_type(
            "Backend Engineer Intern",
            "This position may require a government security clearance.",
        )
        self.assertEqual("backend", role)

    def test_security_role_title_still_selects_security_courses(self) -> None:
        role = tailor.infer_role_type(
            "Application Security Engineer Intern",
            "Review services and collaborate with backend engineers.",
        )
        self.assertEqual("security", role)

    def test_tailor_never_calls_freeform_resume_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tailor, "OUT_DIR", Path(tmp)), \
                mock.patch.object(tailor, "_api", side_effect=AssertionError("model called")), \
                mock.patch.object(tailor, "compile_pdf", return_value=True), \
                mock.patch.object(tailor, "measure_fill", return_value=0.9), \
                mock.patch.object(tailor, "LAST_PAGE_COUNT", 1):
            result = tailor.tailor(
                "test:grounded", "Dmg Media", "AI Engineer Intern",
                "Python and machine learning",
            )
            self.assertEqual(
                Path(tmp) / "Dmg_Media_AI_Engineer_Intern_bcfdfdba2b.pdf",
                result,
            )
            tex = result.with_suffix(".tex").read_text()
            self.assertNotIn("U.S. Citizen", tex)
            quality = result.with_suffix(".quality.json").read_text()
            self.assertIn('"source": "deterministic_grounded"', quality)


if __name__ == "__main__":
    unittest.main()
