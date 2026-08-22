from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deploy.aws.backup_sqlite import backup_database

ROOT = Path(__file__).resolve().parent
AWS = ROOT / "deploy" / "aws"


def hcl_block(text: str, header: str) -> str:
    start = text.index(header)
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"Unclosed HCL block: {header}")


class AwsDeploymentTests(unittest.TestCase):
    def test_instance_security_group_has_no_ingress(self) -> None:
        main = (AWS / "main.tf").read_text()
        security_group = hcl_block(main, 'resource "aws_security_group" "instance"')
        self.assertNotIn("ingress {", security_group)
        self.assertIn("egress {", security_group)

    def test_instance_storage_and_metadata_are_hardened(self) -> None:
        main = (AWS / "main.tf").read_text()
        instance = hcl_block(main, 'resource "aws_instance" "jobhunt"')
        self.assertIn("encrypted             = true", instance)
        self.assertIn('http_tokens                 = "required"', instance)
        self.assertIn("prevent_destroy = true", instance)
        self.assertIn("associate_public_ip_address = true", instance)

    def test_state_bucket_blocks_public_access(self) -> None:
        main = (AWS / "main.tf").read_text()
        block = hcl_block(main, 'resource "aws_s3_bucket_public_access_block" "state"')
        for setting in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        ):
            self.assertRegex(block, rf"{setting}\s*=\s*true")

    def test_bootstrap_refuses_live_local_workers(self) -> None:
        script = (AWS / "stage-and-bootstrap.sh").read_text()
        guard = script.index("CONFIRM_LOCAL_WORKERS_STOPPED")
        upload = script.index("aws s3 cp")
        self.assertLess(guard, upload)
        self.assertIn("launchctl list", script[guard:upload])

    def test_bootstrap_never_enables_timers(self) -> None:
        script = (AWS / "stage-and-bootstrap.sh").read_text()
        self.assertIn("systemctl disable --now", script)
        self.assertNotIn("systemctl enable --now", script)

    def test_timers_schedule_when_enabled_after_boot(self) -> None:
        timer_directory = ROOT / "deploy" / "systemd"
        timers = sorted(timer_directory.glob("*.timer"))
        self.assertEqual(5, len(timers))
        for timer in timers:
            text = timer.read_text()
            with self.subTest(timer=timer.name):
                self.assertIn("OnActiveSec=", text)
                self.assertIn("OnUnitActiveSec=", text)
                self.assertNotIn("OnBootSec=", text)

    def test_submit_service_has_restart_supervision(self) -> None:
        service = (ROOT / "deploy" / "systemd" / "jobhunt-submit.service").read_text()
        self.assertIn("ExecStart=/opt/jobhunt/.venv/bin/python /opt/jobhunt/submit_daemon.py", service)
        self.assertIn("Restart=always", service)
        self.assertIn("RestartSec=10", service)

    def test_obsolete_submit_timer_is_not_referenced(self) -> None:
        paths = [
            ROOT / "deploy" / "VPS.md",
            AWS / "README.md",
            AWS / "stage-and-bootstrap.sh",
            AWS / "verify-instance.sh",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertNotIn("jobhunt@submit.timer", path.read_text())

    def test_architecture_docs_use_canonical_posting_not_company_freeze(self) -> None:
        readme = (ROOT / "README.md").read_text()
        design = (ROOT / "docs" / "system-design.html").read_text()
        for text in (readme, design):
            self.assertIn("Canonical posting dedupe", text)
            self.assertNotIn("one application per company ever", text)
            self.assertNotIn("one app per company forever", text)
            self.assertNotIn("freezes the company forever", text)

    def test_ssm_scripts_run_remote_checks_in_bash(self) -> None:
        for name in ("stage-and-bootstrap.sh", "verify-instance.sh"):
            script = (AWS / name).read_text()
            self.assertIn("/bin/bash -euo pipefail -c", script)

    def test_anthropic_key_is_not_an_aws_cli_argument(self) -> None:
        script = (AWS / "stage-and-bootstrap.sh").read_text()
        self.assertNotIn('--value "${ANTHROPIC_API_KEY}"', script)
        self.assertIn("--cli-input-json", script)

    def test_instance_reads_only_explicit_jobhunt_parameters(self) -> None:
        iam = (AWS / "iam.tf").read_text()
        policy = hcl_block(iam, 'data "aws_iam_policy_document" "instance_state"')
        self.assertIn("parameter/${var.project_name}/anthropic_api_key", policy)
        self.assertIn("parameter/${var.project_name}/kith_env", policy)
        self.assertIn("parameter/${var.project_name}/application_answers", policy)
        self.assertNotIn("parameter/${var.project_name}/*", policy)

    def test_kith_config_is_restored_with_worker_read_access(self) -> None:
        installer = (ROOT / "deploy" / "install-ubuntu.sh").read_text()
        bootstrap = (AWS / "stage-and-bootstrap.sh").read_text()
        self.assertIn("install -d -o root -g jobhunt -m 0750 /etc/jobhunt", installer)
        self.assertIn("--name ${kith_parameter_name} --with-decryption", bootstrap)
        self.assertIn("chown root:jobhunt /etc/jobhunt/kith.env", bootstrap)
        self.assertIn("chmod 640 /etc/jobhunt/kith.env", bootstrap)

    def test_application_answers_are_restored_with_worker_read_access(self) -> None:
        bootstrap = (AWS / "stage-and-bootstrap.sh").read_text()
        self.assertIn("--name ${application_answers_parameter_name} --with-decryption", bootstrap)
        self.assertIn("chown root:jobhunt /etc/jobhunt/application_answers.yaml", bootstrap)
        self.assertIn("chmod 640 /etc/jobhunt/application_answers.yaml", bootstrap)
        self.assertIn("JOBHUNT_APPLICATION_ANSWERS_FILE=/etc/jobhunt/application_answers.yaml", bootstrap)

    def test_sqlite_staging_is_consistent_and_removes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "state" / "tracker.db"
            with sqlite3.connect(source) as database:
                database.execute("CREATE TABLE postings (id INTEGER PRIMARY KEY, title TEXT)")
                database.execute("INSERT INTO postings (title) VALUES ('Engineer')")

            destination.parent.mkdir(parents=True)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{destination}{suffix}").write_bytes(b"stale")

            backup_database(source, destination)

            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as database:
                self.assertEqual(
                    database.execute("SELECT title FROM postings").fetchone(),
                    ("Engineer",),
                )
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{destination}{suffix}").exists())


if __name__ == "__main__":
    unittest.main()
