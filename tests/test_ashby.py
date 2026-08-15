import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
from ashby import _fill_basics  # noqa: E402


class _Element:
    def __init__(self, present=True, value=""):
        self.first = self
        self.present = present
        self.value = value

    def count(self):
        return int(self.present)

    def is_visible(self):
        return self.present

    def input_value(self):
        return self.value

    def fill(self, value):
        self.value = value


class _SplitNamePage:
    def __init__(self):
        self.fields = {
            "First Name": _Element(),
            "Last Name": _Element(),
            "Email": _Element(),
            "Phone": _Element(),
            "LinkedIn": _Element(),
            "GitHub": _Element(),
        }
        self.calls = []

    def get_by_label(self, label, exact=False):
        self.calls.append((label, exact))
        return self.fields.get(label, _Element(present=False))


class AshbyBasicFieldTests(unittest.TestCase):
    PROFILE = {
        "name": {"first": "David", "last": "Cui"},
        "email": "david@example.com",
        "phone": "555-0100",
        "links": {
            "linkedin": "https://www.linkedin.com/in/david",
            "github": "https://github.com/david",
        },
    }

    def test_split_name_form_fills_both_names_without_full_name_fallback(self):
        page = _SplitNamePage()

        _fill_basics(page, self.PROFILE)

        self.assertEqual("David", page.fields["First Name"].value)
        self.assertEqual("Cui", page.fields["Last Name"].value)
        self.assertIn(("Name", True), page.calls)

    def test_existing_value_is_not_overwritten(self):
        page = _SplitNamePage()
        page.fields["First Name"].value = "Already Present"

        _fill_basics(page, self.PROFILE)

        self.assertEqual("Already Present", page.fields["First Name"].value)
        self.assertEqual("Cui", page.fields["Last Name"].value)

    def test_combined_name_form_remains_supported(self):
        page = _SplitNamePage()
        page.fields = {"Name": _Element()}

        _fill_basics(page, self.PROFILE)

        self.assertEqual("David Cui", page.fields["Name"].value)


if __name__ == "__main__":
    unittest.main()
