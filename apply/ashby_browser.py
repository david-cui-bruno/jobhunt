from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import os

PROFILE_DIR = Path(".jobhunt-browser-profiles") / "ashby"
DEFAULT_CHROME_FOR_TESTING = Path("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
DEFAULT_STABLE_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@dataclass(frozen=True)
class ChromeTarget:
    executable: Path
    process_name: str
    dedicated: bool


def _classify_chrome_path(path: Path) -> ChromeTarget:
    parts = set(path.parts)
    name = path.name
    if name == "Google Chrome for Testing" or "Google Chrome for Testing.app" in parts:
        return ChromeTarget(path, "Google Chrome for Testing", True)
    if name == "Google Chrome" or "Google Chrome.app" in parts:
        return ChromeTarget(path, "Google Chrome", False)
    raise RuntimeError(
        f"Ambiguous Chrome executable path {path}. Configure Chrome for Testing or a Google Chrome app bundle."
    )


def _is_process_running(process_name: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-x", process_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _validate_target_is_safe(target: ChromeTarget) -> ChromeTarget:
    if not target.dedicated and _is_process_running(target.process_name):
        raise RuntimeError(
            "Stable Google Chrome is already running. install or configure Chrome for Testing with "
            "JOBHUNT_ASHBY_CHROME_PATH before running Ashby hidden browser automation."
        )
    return target


def resolve_chrome() -> ChromeTarget:
    explicit = os.environ.get("JOBHUNT_ASHBY_CHROME_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise RuntimeError(f"Configured Chrome executable does not exist: {path}")
        return _validate_target_is_safe(_classify_chrome_path(path))

    if DEFAULT_CHROME_FOR_TESTING.exists():
        return _classify_chrome_path(DEFAULT_CHROME_FOR_TESTING)
    if DEFAULT_STABLE_CHROME.exists():
        return _validate_target_is_safe(_classify_chrome_path(DEFAULT_STABLE_CHROME))
    raise RuntimeError(
        "No usable Chrome binary found. Install Chrome for Testing or set JOBHUNT_ASHBY_CHROME_PATH "
        "to Chrome for Testing."
    )


def start_hide_watchdog(target: ChromeTarget, *, timeout_seconds: float = 5.0) -> subprocess.Popen:
    helper_path = Path(__file__).with_name("hide_macos_browser.py")
    command = [sys.executable, str(helper_path), target.process_name, str(timeout_seconds)]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def persistent_ashby_context(
    pw,
    *,
    profile_dir: Path = PROFILE_DIR,
) -> Iterator[object]:
    target = resolve_chrome()
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    start_hide_watchdog(target)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        executable_path=str(target.executable),
    )
    try:
        yield ctx
    finally:
        ctx.close()
