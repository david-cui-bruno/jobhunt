import json
from pathlib import Path

import pytest

import scripts.verify_ashby_browser as verify
from apply.ashby_browser import ChromeTarget


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.closed = False

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.urls = []

    def goto(self, url):
        self.urls.append(url)

    def evaluate(self, script):
        if script == "navigator.userAgent":
            return "Fake UA"
        if script == "navigator.webdriver":
            return False
        if script == "navigator.plugins.length":
            return 3
        raise AssertionError(f"unexpected script {script!r}")


class FakePersistent:
    def __init__(self, context):
        self.context = context
        self.entered = 0
        self.exited = 0

    def __call__(self, _pw):
        return self

    def __enter__(self):
        self.entered += 1
        return self.context

    def __exit__(self, exc_type, exc, tb):
        self.exited += 1
        self.context.close()
        return False


def test_check_only_reports_dedicated_target_without_launch_or_visibility(monkeypatch, capsys):
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    monkeypatch.setattr(verify, "_visibility_for_process", lambda name: pytest.fail("visibility should not run"))
    monkeypatch.setattr(verify, "_run_about_blank", lambda target: pytest.fail("browser should not launch"))

    assert verify.main(["--check-only", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mode": "check-only",
        "ok": True,
        "target": {
            "dedicated": True,
            "executable_path": str(target.executable),
            "process_name": "Google Chrome for Testing",
        },
    }


def test_check_only_reports_structured_fail_closed_blocker(monkeypatch, capsys):
    monkeypatch.setattr(verify, "resolve_chrome", lambda: (_ for _ in ()).throw(RuntimeError("Stable Google Chrome is already running")))
    assert verify.main(["--check-only", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["blocker"] == {"code": "fail_closed", "message": "Stable Google Chrome is already running"}


def test_about_blank_rejects_non_dedicated_before_launch(monkeypatch):
    target = ChromeTarget(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), "Google Chrome", False)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    monkeypatch.setattr(verify, "_run_about_blank", lambda target: pytest.fail("browser should not launch"))
    assert verify.main(["--about-blank", "--json"]) == 1


def test_about_blank_uses_persistent_context_once_and_only_navigates_about_blank(monkeypatch, capsys):
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    context = FakeContext()
    persistent = FakePersistent(context)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    monkeypatch.setattr(verify, "persistent_ashby_context", persistent)
    monkeypatch.setattr(verify, "_sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(verify, "_visibility_for_process", lambda name: {"process_name": name, "visible": False})

    assert verify.main(["--about-blank", "--json"]) == 0

    assert persistent.entered == 1
    assert persistent.exited == 1
    assert context.closed is True
    assert context.pages[0].urls == ["about:blank"]
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"mode", "ok", "executable_path", "process_name", "profile_path", "user_agent", "navigator_webdriver", "plugin_count", "process_visibility"}
    assert payload["process_visibility"]["process_name"] == "Google Chrome for Testing"


def test_visibility_query_error_fails_closed_as_query_error(monkeypatch, capsys):
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    monkeypatch.setattr(verify, "persistent_ashby_context", FakePersistent(FakeContext()))
    monkeypatch.setattr(verify, "_sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(verify, "_visibility_for_process", lambda name: (_ for _ in ()).throw(RuntimeError("process not found: Google Chrome for Testing")))

    assert verify.main(["--about-blank", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "about-blank"
    assert payload["ok"] is False
    assert payload["blocker"] == {"code": "fail_closed", "reason": "query_error", "message": "process not found: Google Chrome for Testing"}
    assert "process_visibility" not in payload


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def test_approved_fields_do_not_read_or_print_sensitive_data(monkeypatch, tmp_path, capsys):
    secret_profile = tmp_path / "profile"
    secret_profile.mkdir()
    (secret_profile / "Cookies").write_text("cookie=secret answer-bank-token")
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr(verify, "PROFILE_DIR", secret_profile)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    monkeypatch.setattr(verify, "persistent_ashby_context", FakePersistent(FakeContext()))
    monkeypatch.setattr(verify, "_sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(verify, "_visibility_for_process", lambda name: {"process_name": name, "visible": False})

    assert verify.main(["--about-blank", "--json"]) == 0
    out = capsys.readouterr().out
    assert "secret" not in out
    assert "cookie" not in out.lower()
    assert "answer-bank" not in out


def test_json_mode_emits_single_object(monkeypatch, capsys):
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)
    assert verify.main(["--check-only", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert isinstance(json.loads(out), dict)


def test_human_check_only_output_is_readable_not_raw_dict(monkeypatch, capsys):
    target = ChromeTarget(Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"), "Google Chrome for Testing", True)
    monkeypatch.setattr(verify, "resolve_chrome", lambda: target)

    assert verify.main(["--check-only"]) == 0

    out = capsys.readouterr().out
    assert "check-only: ok" in out
    assert "target.dedicated: True" in out
    assert "target.executable_path:" in out
    assert "target.process_name: Google Chrome for Testing" in out
    assert "{'" not in out


def test_visibility_output_is_sanitized_by_production_constructor(monkeypatch):
    import apply.hide_macos_browser as hide

    monkeypatch.setattr(hide, "visible_windows_for_process", lambda name: ["secret-cookie-window", "answer-bank"])

    payload = verify._visibility_for_process("Google Chrome for Testing")

    assert payload == {"process_name": "Google Chrome for Testing", "visible": True, "window_count": 2}
    assert "secret-cookie-window" not in json.dumps(payload)
    assert "answer-bank" not in json.dumps(payload)


def test_invalid_or_conflicting_flags_fail_without_launch(monkeypatch):
    monkeypatch.setattr(verify, "resolve_chrome", lambda: pytest.fail("resolver should not run"))
    monkeypatch.setattr(verify, "_run_about_blank", lambda target: pytest.fail("browser should not launch"))
    assert verify.main(["--check-only", "--about-blank", "--json"]) == 2
    assert verify.main(["--json"]) == 2
