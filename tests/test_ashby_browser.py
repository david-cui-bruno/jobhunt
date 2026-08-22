from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from apply.ashby_browser import ChromeTarget, persistent_ashby_context, resolve_chrome, start_hide_watchdog
from apply.hide_macos_browser import APPLESCRIPT, build_osascript_command, process_name_matches


class FakeContext:
    def __init__(self) -> None:
        self.close_calls = 0
        self.pages = []

    def close(self) -> None:
        self.close_calls += 1


class FakeChromium:
    def __init__(self, events: list[str], context: FakeContext) -> None:
        self.events = events
        self.context = context
        self.launch_kwargs: dict[str, Any] | None = None

    def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.events.append("launch")
        self.launch_kwargs = kwargs
        return self.context


class FakePlaywright:
    def __init__(self, events: list[str], context: FakeContext) -> None:
        self.chromium = FakeChromium(events, context)


def make_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_explicit_chrome_for_testing_path_is_preferred_and_dedicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = make_binary(tmp_path / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing")
    monkeypatch.setenv("JOBHUNT_ASHBY_CHROME_PATH", str(testing))

    target = resolve_chrome()

    assert target == ChromeTarget(testing, "Google Chrome for Testing", True)


def test_default_chrome_for_testing_wins_over_stable_chrome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = make_binary(tmp_path / "Applications" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing")
    stable = make_binary(tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    monkeypatch.delenv("JOBHUNT_ASHBY_CHROME_PATH", raising=False)
    monkeypatch.setattr("apply.ashby_browser.DEFAULT_CHROME_FOR_TESTING", testing)
    monkeypatch.setattr("apply.ashby_browser.DEFAULT_STABLE_CHROME", stable)

    target = resolve_chrome()

    assert target == ChromeTarget(testing, "Google Chrome for Testing", True)


def test_missing_binaries_produce_clear_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBHUNT_ASHBY_CHROME_PATH", raising=False)
    monkeypatch.setattr("apply.ashby_browser.DEFAULT_CHROME_FOR_TESTING", tmp_path / "missing-testing")
    monkeypatch.setattr("apply.ashby_browser.DEFAULT_STABLE_CHROME", tmp_path / "missing-stable")

    with pytest.raises(RuntimeError, match="Chrome for Testing"):
        resolve_chrome()


def test_stable_chrome_is_accepted_only_when_normal_chrome_is_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stable = make_binary(tmp_path / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    monkeypatch.setenv("JOBHUNT_ASHBY_CHROME_PATH", str(stable))
    monkeypatch.setattr("apply.ashby_browser._is_process_running", lambda name: False)

    target = resolve_chrome()

    assert target == ChromeTarget(stable, "Google Chrome", False)


def test_stable_chrome_is_rejected_when_running_even_with_shared_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stable = make_binary(tmp_path / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome")
    monkeypatch.setenv("JOBHUNT_ASHBY_CHROME_PATH", str(stable))
    monkeypatch.setenv("JOBHUNT_ASHBY_ALLOW_SHARED_CHROME", "1")
    monkeypatch.setattr("apply.ashby_browser._is_process_running", lambda name: name == "Google Chrome")

    with pytest.raises(RuntimeError, match="install or configure Chrome for Testing"):
        resolve_chrome()


def test_ambiguous_explicit_executable_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = make_binary(tmp_path / "Chromium")
    monkeypatch.setenv("JOBHUNT_ASHBY_CHROME_PATH", str(binary))

    with pytest.raises(RuntimeError, match="Ambiguous"):
        resolve_chrome()


def test_watchdog_starts_before_persistent_context_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = ChromeTarget(make_binary(tmp_path / "Google Chrome for Testing"), "Google Chrome for Testing", True)
    events: list[str] = []
    context = FakeContext()
    fake_pw = FakePlaywright(events, context)
    monkeypatch.setattr("apply.ashby_browser.resolve_chrome", lambda: target)
    monkeypatch.setattr("apply.ashby_browser.start_hide_watchdog", lambda seen_target: events.append(f"watchdog:{seen_target.process_name}"))

    with persistent_ashby_context(fake_pw, profile_dir=tmp_path / "profile"):
        pass

    assert events[:2] == ["watchdog:Google Chrome for Testing", "launch"]


def test_persistent_launch_receives_real_browser_arguments_and_no_spoofing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = ChromeTarget(make_binary(tmp_path / "Google Chrome for Testing"), "Google Chrome for Testing", True)
    context = FakeContext()
    fake_pw = FakePlaywright([], context)
    profile_dir = tmp_path / "profile"
    monkeypatch.setattr("apply.ashby_browser.resolve_chrome", lambda: target)
    monkeypatch.setattr("apply.ashby_browser.start_hide_watchdog", lambda seen_target: None)

    with persistent_ashby_context(fake_pw, profile_dir=profile_dir):
        pass

    kwargs = fake_pw.chromium.launch_kwargs
    assert kwargs is not None
    assert kwargs["headless"] is False
    assert kwargs["executable_path"] == str(target.executable)
    assert kwargs["user_data_dir"] == str(profile_dir)
    forbidden = {"user_agent", "locale", "timezone_id", "geolocation", "permissions", "extra_http_headers", "args"}
    assert forbidden.isdisjoint(kwargs)
    assert profile_dir.is_dir()
    assert (profile_dir.stat().st_mode & 0o777) == 0o700


def test_context_closes_on_normal_and_exceptional_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = ChromeTarget(make_binary(tmp_path / "Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr("apply.ashby_browser.resolve_chrome", lambda: target)
    monkeypatch.setattr("apply.ashby_browser.start_hide_watchdog", lambda seen_target: None)

    normal = FakeContext()
    with persistent_ashby_context(FakePlaywright([], normal), profile_dir=tmp_path / "normal"):
        pass
    assert normal.close_calls == 1

    exceptional = FakeContext()
    with pytest.raises(ValueError):
        with persistent_ashby_context(FakePlaywright([], exceptional), profile_dir=tmp_path / "exceptional"):
            raise ValueError("boom")
    assert exceptional.close_calls == 1


def test_watchdog_command_construction_uses_argv_and_no_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            calls.append({"cmd": cmd, "kwargs": kwargs})

    monkeypatch.setattr("apply.ashby_browser.subprocess.Popen", FakePopen)
    target = ChromeTarget(tmp_path / "Google Chrome for Testing", "Google Chrome for Testing", True)

    proc = start_hide_watchdog(target, timeout_seconds=1.25)

    assert proc is not None
    assert len(calls) == 1
    assert isinstance(calls[0]["cmd"], list)
    assert "Google Chrome for Testing" in calls[0]["cmd"]
    assert calls[0]["kwargs"].get("shell") is not True


def test_helper_command_and_matching_are_exact() -> None:
    cmd = build_osascript_command("Google Chrome for Testing")

    assert isinstance(cmd, list)
    assert cmd[-1] == "Google Chrome for Testing"
    assert "processName to item 1 of argv" in APPLESCRIPT
    assert "process processName" in APPLESCRIPT
    assert process_name_matches("Google Chrome for Testing", "Google Chrome for Testing") is True
    assert process_name_matches("Google Chrome", "Google Chrome for Testing") is False
    assert process_name_matches("Google Chrome for Testing", "Google Chrome") is False


def test_profile_paths_are_ignored() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert ".jobhunt-browser-profiles/" in gitignore
    assert "out/browser-profiles/" in gitignore
