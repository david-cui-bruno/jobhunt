from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import os

PROFILE_DIR = Path(".jobhunt-browser-profiles") / "ashby"
DEFAULT_CHROME_FOR_TESTING = Path("/Applications/Ashby Chrome for Testing.app/Contents/MacOS/Ashby Chrome for Testing")
DEFAULT_STABLE_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@dataclass(frozen=True)
class ChromeTarget:
    executable: Path
    process_name: str
    dedicated: bool


def _classify_chrome_path(path: Path) -> ChromeTarget:
    parts = set(path.parts)
    name = path.name
    ashby_testing_signal = name == "Ashby Chrome for Testing" or "Ashby Chrome for Testing.app" in parts
    generic_testing_signal = name == "Google Chrome for Testing" or "Google Chrome for Testing.app" in parts
    testing_signal = ashby_testing_signal or generic_testing_signal
    stable_signal = name == "Google Chrome" or "Google Chrome.app" in parts
    if testing_signal and stable_signal:
        raise RuntimeError(
            f"Conflicting Chrome executable path {path}. Bundle name and executable name disagree."
        )
    if testing_signal:
        process_name = "Ashby Chrome for Testing" if ashby_testing_signal else "Google Chrome for Testing"
        return ChromeTarget(path, process_name, True)
    if stable_signal:
        return ChromeTarget(path, "Google Chrome", False)
    raise RuntimeError(
        f"Ambiguous Chrome executable path {path}. Configure Chrome for Testing or a Google Chrome app bundle."
    )


def _is_process_running(process_name: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", process_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"Unable to check whether {process_name!r} is running: {exc}") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        f"pgrep failed while checking whether {process_name!r} is running with return code {result.returncode}."
    )


def _stop_watchdog(proc: subprocess.Popen, *, timeout_seconds: float = 1.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_seconds)


def _validate_target_is_safe(target: ChromeTarget) -> ChromeTarget:
    if target.dedicated and _is_process_running(target.process_name):
        raise RuntimeError(
            f"Dedicated Chrome target process {target.process_name!r} is already running. Stop that exact "
            "process before running Ashby hidden browser automation."
        )
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
        return _validate_target_is_safe(_classify_chrome_path(DEFAULT_CHROME_FOR_TESTING))
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


def ensure_hidden_after_launch(target: ChromeTarget, *, timeout_seconds: float = 5.0) -> None:
    helper_path = Path(__file__).with_name("hide_macos_browser.py")
    command = [sys.executable, str(helper_path), target.process_name, str(timeout_seconds)]
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "hide helper failed").strip()
        raise RuntimeError(
            f"Dedicated Chrome process {target.process_name!r} could not be hidden after launch: {detail}"
        )


@contextmanager
def persistent_ashby_context(
    pw,
    *,
    profile_dir: Path = PROFILE_DIR,
) -> Iterator[object]:
    target = resolve_chrome()
    if not target.dedicated:
        raise RuntimeError("Ashby browser automation requires a dedicated Chrome for Testing target.")
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile_dir.chmod(0o700)
    watchdog = start_hide_watchdog(target)
    ctx = None
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            executable_path=str(target.executable),
        )
        ensure_hidden_after_launch(target)
        yield ctx
    except BaseException:
        with suppress(Exception):
            if ctx is not None:
                ctx.close()
        with suppress(Exception):
            _stop_watchdog(watchdog)
        raise
    else:
        try:
            if ctx is not None:
                ctx.close()
        finally:
            with suppress(Exception):
                _stop_watchdog(watchdog)
