from __future__ import annotations

import subprocess

import pytest

from apply import hide_macos_browser as hide


def test_visibility_command_targets_exact_supplied_process_name():
    command = hide.build_visible_windows_command("Google Chrome for Testing")

    assert command[-1] == "Google Chrome for Testing"
    assert "Google Chrome" not in command[:-1]
    script = command[2]
    assert "processName" in script
    assert "Google Chrome" not in script
    assert "System Events" in script
    assert "visible of process processName" in script
    assert "count of windows of process processName" in script
    assert "visible of candidateWindow" not in script


def test_visible_windows_uses_constructed_command_without_invoking_system_events(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "visible=false\nwindow_count=0\n", "")

    monkeypatch.setattr(hide.subprocess, "run", fake_run)

    assert hide.visible_windows_for_process("Google Chrome for Testing") == []
    assert calls[0][0][-1] == "Google Chrome for Testing"
    assert calls[0][0][0] == "osascript"


def test_visibility_query_returns_synthetic_windows_from_process_level_evidence(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "visible=true\nwindow_count=3\n", "")

    monkeypatch.setattr(hide.subprocess, "run", fake_run)

    assert hide.visible_windows_for_process("Google Chrome for Testing") == ["<process-visible-window>"] * 3


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, "", "System Events denied", "System Events visibility query failed"),
        (0, "missing\n", "", "process not found"),
        (0, "visible=yes\nwindow_count=1\n", "", "malformed visibility query output"),
        (0, "visible=false\nwindow_count=not-int\n", "", "malformed visibility query output"),
    ],
)
def test_visibility_query_failures_raise_safe_error(monkeypatch, returncode, stdout, stderr, message):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(hide.subprocess, "run", fake_run)

    with pytest.raises(hide.VisibilityQueryError, match=message):
        hide.visible_windows_for_process("Google Chrome for Testing")


def test_hide_helper_fails_when_process_never_becomes_hideable(monkeypatch):
    monkeypatch.setattr(hide, "hide_once", lambda process_name: "waiting")

    assert hide.main(["Ashby Chrome for Testing", "0"]) == 1
