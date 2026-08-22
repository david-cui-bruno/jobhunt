from apply import hide_macos_browser as hide


def test_visibility_command_targets_exact_supplied_process_name():
    command = hide.build_visible_windows_command("Google Chrome for Testing")

    assert command[-1] == "Google Chrome for Testing"
    assert "Google Chrome" not in command[:-1]
    script = command[2]
    assert "processName" in script
    assert "Google Chrome" not in script
    assert "System Events" in script


def test_visible_windows_uses_constructed_command_without_invoking_system_events(monkeypatch):
    calls = []

    class Result:
        stdout = "visible\n"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(hide.subprocess, "run", fake_run)

    assert hide.visible_windows_for_process("Google Chrome for Testing") == ["visible"]
    assert calls[0][0][-1] == "Google Chrome for Testing"
    assert calls[0][0][0] == "osascript"
