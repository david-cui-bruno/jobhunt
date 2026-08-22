from __future__ import annotations

import subprocess
import sys
import time

APPLESCRIPT = """on run argv
  set processName to item 1 of argv
  tell application "System Events"
    if exists process processName then
      set visible of process processName to false
      return "hidden"
    end if
  end tell
  return "waiting"
end run
"""

VISIBILITY_APPLESCRIPT = """on run argv
  set processName to item 1 of argv
  tell application "System Events"
    if not (exists process processName) then
      return "missing"
    end if
    set processVisible to visible of process processName
    set windowCount to count of windows of process processName
    return "visible=" & processVisible & linefeed & "window_count=" & windowCount
  end tell
end run
"""


class VisibilityQueryError(RuntimeError):
    """Safe fail-closed visibility evidence error."""


def build_osascript_command(process_name: str) -> list[str]:
    return ["osascript", "-e", APPLESCRIPT, process_name]


def build_visible_windows_command(process_name: str) -> list[str]:
    return ["osascript", "-e", VISIBILITY_APPLESCRIPT, process_name]


def visible_windows_for_process(process_name: str) -> list[str]:
    result = subprocess.run(
        build_visible_windows_command(process_name),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "osascript failed").strip()
        raise VisibilityQueryError(f"System Events visibility query failed: {detail}")
    evidence = _parse_visibility_output(result.stdout, process_name)
    if not evidence["visible"]:
        return []
    return ["<process-visible-window>"] * evidence["window_count"]


def _parse_visibility_output(stdout: str, process_name: str) -> dict[str, int | bool]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines == ["missing"]:
        raise VisibilityQueryError(f"process not found: {process_name}")
    parsed: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            raise VisibilityQueryError(f"malformed visibility query output: {line}")
        key, value = line.split("=", 1)
        parsed[key] = value
    if set(parsed) != {"visible", "window_count"} or parsed["visible"] not in {"true", "false"}:
        raise VisibilityQueryError(f"malformed visibility query output: {stdout.strip()}")
    try:
        window_count = int(parsed["window_count"])
    except ValueError as exc:
        raise VisibilityQueryError(f"malformed visibility query output: {stdout.strip()}") from exc
    if window_count < 0:
        raise VisibilityQueryError(f"malformed visibility query output: {stdout.strip()}")
    return {"visible": parsed["visible"] == "true", "window_count": window_count}


def hide_once(process_name: str) -> str:
    result = subprocess.run(
        build_osascript_command(process_name),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: hide_macos_browser.py PROCESS_NAME TIMEOUT_SECONDS", file=sys.stderr)
        return 2
    process_name = argv[0]
    timeout_seconds = float(argv[1])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if hide_once(process_name) == "hidden":
            return 0
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
