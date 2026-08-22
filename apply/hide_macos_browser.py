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


def process_name_matches(candidate: str, target: str) -> bool:
    return candidate == target


def build_osascript_command(process_name: str) -> list[str]:
    return ["osascript", "-e", APPLESCRIPT, process_name]


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
