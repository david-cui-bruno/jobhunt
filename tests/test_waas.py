import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "apply")]
from watcher import waas  # noqa: E402


class _Page:
    def __init__(self):
        self.call = None
        self.control = object()

    def get_by_role(self, role, name=None, exact=False):
        self.call = (role, name, exact)

        class _Locator:
            last = self.control

        return _Locator()


class WorkAtAStartupTests(unittest.TestCase):
    def test_directory_cta_falls_back_to_company_slug(self):
        self.assertEqual(
            "Bluejay",
            waas._company_name(
                "See all 8 jobs ›",
                "https://www.workatastartup.com/companies/bluejay",
            ),
        )

    def test_real_company_label_is_preserved(self):
        self.assertEqual(
            "Candle (F24)",
            waas._company_name(
                "Candle (F24)",
                "https://www.workatastartup.com/companies/candle",
            ),
        )

    def test_note_prompt_uses_track_graduation_placeholder(self):
        # David 2026-08-19: grad date is track-based (intern May 2028 /
        # full-time May 2027), injected per-application via {grad_date}.
        self.assertIn("expected {grad_date}", waas.NOTE_PROMPT)
        self.assertNotIn("June 2028", waas.NOTE_PROMPT)
        self.assertNotIn("Brown CS+Econ '27", waas.NOTE_PROMPT)

    def test_final_send_selector_cannot_match_background_apply(self):
        page = _Page()
        control = waas._send_button(page)
        self.assertIs(control, page.control)
        self.assertEqual(("button", "Send", True), page.call)


if __name__ == "__main__":
    unittest.main()
