from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import submit


class SubmitSafetyTests(unittest.TestCase):
    def test_both_dry_run_spellings_are_safe(self) -> None:
        self.assertTrue(submit._dry_run_requested(["submit.py", "--dry"]))
        self.assertTrue(submit._dry_run_requested(["submit.py", "--dry-run"]))
        self.assertFalse(submit._dry_run_requested(["submit.py"]))

    def test_cli_dry_run_invokes_submitter_without_live_writes(self) -> None:
        with mock.patch.object(submit, "submit_ready", return_value=[]) as submit_ready:
            submit.main(["submit.py", "--dry-run"])

        submit_ready.assert_called_once_with(dry_run=True)

    def test_runtime_path_relocates_laptop_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relocated = root / "out" / "resumes" / "candidate.pdf"
            relocated.parent.mkdir(parents=True)
            relocated.write_bytes(b"pdf")

            stale = "/Users/old-user/jobhunt/out/resumes/candidate.pdf"
            self.assertEqual(submit._runtime_path(stale, root), relocated)


if __name__ == "__main__":
    unittest.main()
