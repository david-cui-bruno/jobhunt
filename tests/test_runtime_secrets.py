from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

import pytest

import runtime_secrets
from runtime_secrets import ALLOWED_SECRET_KEYS, load_runtime_env
from scripts.migrate_launchd_secrets import migrate


PLIST_NAMES_TO_SANITIZE = {
    "com.jobhunt.drip.plist": "/Users/davidcui824/jobhunt/drip.py",
    "com.jobhunt.inbox.plist": "/Users/davidcui824/jobhunt/inbox.py",
    "com.jobhunt.revise.plist": "/Users/davidcui824/jobhunt/revise.py",
    "com.jobhunt.sprint.plist": "/Users/davidcui824/jobhunt/sprint.py",
    "com.jobhunt.submit.plist": "/Users/davidcui824/jobhunt/submit_daemon.py",
}
EXPECTED_PREFIX = [
    "/usr/bin/python3",
    "/Users/davidcui824/jobhunt/runtime_secrets.py",
    "--",
    "/usr/bin/python3",
]


def write_env(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


def write_plist(path: Path, environment: dict[str, str], args: list[str] | None = None) -> None:
    payload: dict[str, object] = {"EnvironmentVariables": environment}
    if args is not None:
        payload["ProgramArguments"] = args
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def test_runtime_env_requires_owner_only_permissions(tmp_path: Path) -> None:
    path = write_env(tmp_path / "runtime.env", "ANTHROPIC_API_KEY=fake-secret\n", 0o644)

    with pytest.raises(PermissionError):
        load_runtime_env(path)


def test_runtime_env_rejects_non_current_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_env(tmp_path / "runtime.env", "ANTHROPIC_API_KEY=fake-secret\n")
    real_stat = os.stat

    class StatProxy:
        def __init__(self, result: os.stat_result) -> None:
            self._result = result
            self.st_uid = os.getuid() + 1
            self.st_mode = result.st_mode

        def __getattr__(self, name: str):
            return getattr(self._result, name)

    monkeypatch.setattr(runtime_secrets.os, "stat", lambda target: StatProxy(real_stat(target)))

    with pytest.raises(PermissionError):
        load_runtime_env(path)


def test_runtime_env_rejects_malformed_lines(tmp_path: Path) -> None:
    path = write_env(tmp_path / "runtime.env", "ANTHROPIC_API_KEY=fake-secret\nmalformed\n")

    with pytest.raises(ValueError):
        load_runtime_env(path)


@pytest.mark.parametrize(
    "line",
    [
        "UNKNOWN_KEY=fake-secret\n",
        "ANTHROPIC_API_KEY=fake-secret\nOTHER_SECRET=fake-secret\n",
        " ANTHROPIC_API_KEY=fake-secret\n",
        "ANTHROPIC_API_KEY =fake-secret\n",
        "=fake-secret\n",
    ],
)
def test_runtime_env_rejects_unknown_or_invalid_keys(tmp_path: Path, line: str) -> None:
    path = write_env(tmp_path / "runtime.env", line)

    with pytest.raises(ValueError):
        load_runtime_env(path)


def test_runtime_env_loads_allowlisted_values(tmp_path: Path) -> None:
    path = write_env(tmp_path / "runtime.env", "ANTHROPIC_API_KEY=fake-secret\n")

    assert load_runtime_env(path) == {"ANTHROPIC_API_KEY": "fake-secret"}


def test_wrapper_execs_with_environment_without_logging_or_arg_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_value = "fake-secret-never-log"
    env_path = write_env(tmp_path / "runtime.env", f"ANTHROPIC_API_KEY={secret_value}\n")
    captured: dict[str, object] = {}

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured["file"] = file
        captured["args"] = args
        captured["env_value"] = env["ANTHROPIC_API_KEY"]
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(runtime_secrets.os, "execvpe", fake_execvpe)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        runtime_secrets.main(["--env-file", str(env_path), "--", "/bin/echo", "hello"])

    assert captured == {
        "file": "/bin/echo",
        "args": ["/bin/echo", "hello"],
        "env_value": secret_value,
    }
    output = capsys.readouterr()
    assert secret_value not in output.out
    assert secret_value not in output.err
    assert secret_value not in captured["args"]


def test_migration_writes_mode_600_without_printing_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents / "com.jobhunt.drip.plist", {"ANTHROPIC_API_KEY": "fake-secret", "PATH": "/usr/bin"})
    output = tmp_path / "runtime.env"

    migrate(agents, output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "ANTHROPIC_API_KEY=fake-secret\n" == output.read_text()
    stdout = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in stdout
    assert "fake-secret" not in stdout


def test_migration_requires_identical_values_across_plists(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents / "com.jobhunt.drip.plist", {"ANTHROPIC_API_KEY": "same-secret", "PATH": "/usr/bin"})
    write_plist(agents / "com.jobhunt.inbox.plist", {"ANTHROPIC_API_KEY": "same-secret", "PATH": "/bin"})
    output = tmp_path / "runtime.env"

    migrate(agents, output)

    assert output.read_text() == "ANTHROPIC_API_KEY=same-secret\n"


def test_migration_rejects_conflicting_values_without_output_file(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents / "com.jobhunt.drip.plist", {"ANTHROPIC_API_KEY": "first-secret", "PATH": "/usr/bin"})
    write_plist(agents / "com.jobhunt.inbox.plist", {"ANTHROPIC_API_KEY": "second-secret", "PATH": "/bin"})
    output = tmp_path / "runtime.env"

    with pytest.raises(ValueError):
        migrate(agents, output)

    assert not output.exists()


def test_migration_rejects_unknown_environment_keys(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    write_plist(agents / "com.jobhunt.drip.plist", {"ANTHROPIC_API_KEY": "fake-secret", "UNKNOWN_SECRET": "hidden"})

    with pytest.raises(ValueError):
        migrate(agents, tmp_path / "runtime.env")


def test_committed_jobhunt_plists_are_sanitized_and_preserve_exact_scripts() -> None:
    for plist_name, script_path in PLIST_NAMES_TO_SANITIZE.items():
        with Path("launchd", plist_name).open("rb") as handle:
            payload = plistlib.load(handle)

        environment = payload.get("EnvironmentVariables", {})
        for key in ALLOWED_SECRET_KEYS:
            assert key not in environment
        assert "PATH" in environment
        assert payload["ProgramArguments"] == [*EXPECTED_PREFIX, script_path]


def test_submit_daemon_keepalive_is_preserved() -> None:
    with Path("launchd/com.jobhunt.submit.plist").open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["KeepAlive"] is True
